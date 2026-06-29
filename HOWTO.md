# HOWTO — How & why the memory read works

> An engineering write-up of the reverse-engineering technique behind
> `tbh_inventory.py`. Written for developers who want to understand **why**
> scanning a process's RAM reliably yields the live inventory — and **why no
> fixed memory address is needed**.

---

## TL;DR

TBH stores progress in an **AES-encrypted** save file. We never crack the key.
Instead we exploit one fact:

> **The game has to decrypt its own save to use it — so the plaintext already
> lives in the game's RAM while it runs.** We just read that copy.

There is **no magic address**. Addresses change every launch (ASLR) and move
during garbage collection. So instead of hard-coding an address, we scan the
entire address space for a stable **content signature** — the JSON prefix
`{"commonSaveData":{"version"` — and pull the plaintext straight out.

---

## 1. The problem: an encrypted save

| Layer | Detail |
|---|---|
| File | `SaveFile_Live.es3` |
| Format | Easy Save 3 (ES3) |
| Protection | AES (the save is ciphertext on disk) |
| Game binary | Unity + IL2CPP (obfuscated native code) |

To decrypt the file ourselves we'd need the AES key. It's buried inside an
IL2CPP-obfuscated binary, tangled with runtime-derived state. Recovering it is
doable but painful and brittle across patches. So we don't.

---

## 2. The shortcut: read what the game already decrypted

A game can't render your inventory from ciphertext. At runtime it **must**:

1. Read `SaveFile_Live.es3` from disk.
2. **Decrypt** it with the key (which it already has in its own process).
3. Parse the plaintext JSON into C# objects.
4. Keep those objects (and the source string) **resident in memory** while you play.

That plaintext JSON is the bottleneck we bypass — not the crypto, just the fact
that the plaintext exists in RAM. We become a passive observer of memory the
game legitimately allocated for itself.

```
   SaveFile_Live.es3            TaskBarHero.exe (RAM)
  ┌───────────────┐            ┌──────────────────────────────┐
  │  AES ciphertext│  ──read──▶ │ decrypt() ──▶ plaintext JSON │  ◀── we read THIS
  └───────────────┘   (by the  │                {"commonSave…  │      (read-only)
                       game)    └──────────────────────────────┘
```

---

## 3. Reading another process's memory (Windows API primer)

Three Win32 calls do all the work (`kernel32.dll`, via `ctypes`):

### `OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, pid)`
- Asks Windows for a handle to the game.
- `PROCESS_VM_READ` is **read-only** — we cannot write, inject, or execute.
- **No administrator rights needed**, *provided* we run as the **same Windows
  user** that launched the game. Windows permits same-user cross-process
  inspection; it blocks cross-user unless elevated. (This is exactly why the
  troubleshooting guide says "don't mix admin/non-admin terminals.")

### `VirtualQueryEx(handle, address)` → `MEMORY_BASIC_INFORMATION`
- Enumerates the process's virtual address space **region by region**.
- Each region reports: base address, size, state (`MEM_COMMIT`), type
  (`MEM_PRIVATE`), and protection (`PAGE_*`).
- We filter to regions worth reading:
  - `MEM_COMMIT` — actually backed by physical RAM (not reserved/free).
  - `MEM_PRIVATE` — the process's own heap/buffers, **not** mapped files or
    shared DLL images. The managed heap where our JSON lives is private.
  - Skip `PAGE_NOACCESS` and `PAGE_GUARD` — reading those would fault.

### `ReadProcessMemory(handle, address, buf, size)`
- Copies bytes from the game's VA into our buffer.
- We read in **4 MiB chunks with 1 MiB overlap** so a signature straddling a
  chunk boundary is never missed.

```
  for each committed private region:
      for each 4 MiB chunk (+1 MiB overlap):
          ReadProcessMemory → buffer
          search buffer for the signature
```

---

## 4. Why there is **no magic address** (the key insight)

This is the heart of "why does that address work?" — and the answer is:

> **It doesn't, because there is no fixed address. We search, we don't point.**

Two facts make a hard-coded address impossible:

1. **ASLR (Address Space Layout Randomization).** Every launch, Windows
   randomizes where the heap is mapped. The save string's address differs run to
   run, machine to machine.
2. **.NET garbage collection.** The game is C# (Unity). The GC **compacts and
   moves** heap objects during play. Even within one session, the string's
   address drifts.

So instead of an address, we do **content-addressed retrieval**: we know the
*shape* of the data, and we hunt for that shape across all of RAM. The address
is discovered fresh every run; the content is what's stable.

| Approach | Works? | Why |
|---|---|---|
| Hard-code `0x1A2B3C00` and read it | ❌ | ASLR + GC move it constantly |
| Scan for the content signature | ✅ | The bytes are the same even when the address isn't |

---

## 5. The signature: how we recognize the inventory

### The marker

The decrypted save is a JSON object whose root is always:

```json
{"commonSaveData":{"version": ...
```

That prefix is tied to the **save schema**, not to code or addresses. It only
changes if the developer renames the root field — rare. So we compile it to two
byte signatures and search for both:

```python
SAVE_MARK = b'{"commonSaveData":{"version'
MARK_U16   = SAVE_MARK.decode().encode("utf-16-le")   # C# string form
MARK_U8    = SAVE_MARK                                  # raw ASCII form
```

### Why two encodings?

Because the same JSON exists in RAM in **two forms** simultaneously:

- **UTF-16-LE** — the native C# `string`. Unity/C# stores strings as UTF-16, so
  `{` becomes `7B 00`. This is the primary copy on the managed heap.
- **ASCII/UTF-8** — a raw byte copy, present when the JSON is in a file buffer,
  network buffer, or a serialized/escaped intermediate.

Scanning both maximizes the chance of a hit.

### Extending the match into full JSON

Finding the marker is only the start — we need the **whole** object. From the
marker offset we extend forward as long as bytes look like printable text:

- **UTF-16 runner**: keep reading 2-byte pairs while the high byte is `0x00` and
  the low byte is printable ASCII.
- **ASCII runner**: keep reading single bytes while printable.

Then we validate with `json.loads()`. Among all candidates found, we keep the
**longest valid JSON** — the most complete resident copy. This elegantly handles
partial fragments: a torn/old copy won't parse, a complete one will.

```
RAM:  ... 7B 00 22 00 63 00 6F 00 ...   {"commonSaveData":{"version": 1.00.21, ...
            └ marker (UTF-16) ─┘         └ extend + json.loads → valid? keep longest ─┘
```

---

## 6. Why it's stable across runs and patches

- **The signature is schema-based**, so it survives the game's own updates —
  item keys and balance changes don't alter `{"commonSaveData":...`.
- **No offsets, no pointers, no structure assumptions** beyond the text prefix.
  We don't walk Unity's object graph or chase IL2CPP type metadata. That's the
  robustness win: we sidestep everything brittle.
- **Validation as a filter.** Requiring `json.loads` to succeed means we never
  return garbage; only a genuine, complete save object qualifies.

If a future patch *did* rename the root field, the only fix is one line — update
`SAVE_MARK`. That's it.

---

## 7. Why "open your inventory in-game once" matters

The plaintext isn't guaranteed to be resident the instant the game boots — the
game may keep only a decompressed/decoded view in memory when needed. Opening the
inventory tab **forces the game to deserialize and hydrate the full save** into
resident objects and strings, dramatically increasing the odds the complete JSON
is sitting in a readable committed region when we scan. Hence the prerequisite.

---

## 8. Why this is not a cheat

- **Read-only handle.** `PROCESS_VM_READ` cannot write or execute. We never
  modify a byte of the game or its save.
- **No inputs, no automation, no network to game servers.** Pure passive
  observation of our own process.
- **Zero in-game advantage.** Knowing what you already own, plus public market
  prices, confers no gameplay benefit — it's the same info the game shows you,
  just aggregated so you don't count by hand.

The market-price half (`market_scan.py` / `priceoverview`) is unrelated to memory
RE — it's ordinary HTTPS to Steam's public storefront.

---

## 9. Recreating it yourself

Minimal recipe (any language with Win32 access):

1. Find the target PID (`tasklist` / `EnumProcesses` / `CreateToolhelp32Snapshot`).
2. `OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, pid)`.
3. Loop `VirtualQueryEx` from address `0` upward over committed private regions.
4. `ReadProcessMemory` each region in chunked, overlapping reads.
5. `mem.find(marker)` for your UTF-16 **and** ASCII signatures.
6. Extend each hit into a printable run, `json.loads` to validate, keep the longest.
7. Parse the object, map `ItemKey`s to names, done.

Generalize it: the same technique recovers **any** plaintext a process keeps
resident — decrypted configs, chat buffers, credentials in memory, etc. The
discipline is always: pick a stable content signature, scan, validate.

---

## 10. References

- [Process Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/procthread/process-security-and-access-rights) — `PROCESS_VM_READ`
- [`VirtualQueryEx`](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex) — walking VA regions
- [`ReadProcessMemory`](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory) — cross-process read
- [Easy Save 3](https://docs.moodkie.com/easy-save-3/) — the save format TBH uses
- [Unity IL2CPP](https://docs.unity3d.com/6000.0/Documentation/Manual/scripting-backends-il2cpp.html) — why the binary is obfuscated native code

---

*Educational documentation of a read-only introspection technique. Use only on
software and data you own, within applicable terms of service.*
