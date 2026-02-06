---
name: Parallel file processing analysis
overview: Analysis of per-file work, shared resources, and bottlenecks to determine whether and how to add parallel processing (with a concurrency limit) to the import/update flows.
todos: []
isProject: false
---

# Parallel file processing: work types, limitations, and bottleneck analysis

## Types of work done per file (import flow)

For each file, the pipeline does the following (see [elodie.py](elodie.py) `_import` → `import_file` and [elodie/filesystem.py](elodie/filesystem.py) `process_file`):

```mermaid
sequenceDiagram
    participant Loop
    participant Media
    participant ExifTool
    participant Db
    participant FS
    participant Plugins

    Loop->>Media: get_class_by_file, get_metadata
    Media->>ExifTool: get_metadata(source)
    ExifTool-->>Media: EXIF dict
    Loop->>FS: process_file(...)
    FS->>FS: process_checksum
    FS->>Db: new Db() load hash.json
    FS->>Db: checksum(file) full file read
    FS->>Db: check_hash, get_hash
    FS->>Plugins: run_all_before
    FS->>FS: get_folder_path, get_file_name, create_directory
    FS->>Media: set_original_name
    Media->>ExifTool: set_tags(tags, source)
    FS->>FS: copy or move file
    FS->>Db: new Db() load hash.json, add_hash, update_hash_db (write)
    FS->>Plugins: run_all_after
```



- **ExifTool (singleton subprocess)**  
  - **Read**: `Media.get_metadata()` → `ExifTool().get_metadata(source)` → one `execute_json(filename)` per file (request/response over stdin/stdout).  
  - **Write**: `media.set_original_name()` → `ExifTool().set_tags(tags, source)` → one `execute()` per file.  
  - Optional: geolocation (e.g. [elodie/geolocation.py](elodie/geolocation.py)) uses the same ExifTool for place/coordinate lookups when MapQuest is not configured.
- **Hash DB (JSON file)**  
  - `process_checksum()`: creates a new `Db()`, which **loads the full `hash.json**` from disk; then `db.checksum(file)` (full file read + SHA256); then `check_hash` / `get_hash` (in-memory).  
  - After copy/move: `db = Db()` again (another **full load** of `hash.json`), `db.add_hash(checksum, dest_path)`, then `**db.update_hash_db()**` which **writes the entire `hash.json**` to disk ([elodie/localstorage.py](elodie/localstorage.py) lines 199–205).  
  So: **2 full reads of hash.json + 1 full write of hash.json per file**. No shared in-memory DB; each `Db()` is a new instance that reloads from disk.
- **Disk I/O**  
  - Checksum: full read of source file.  
  - Copy or move: `shutil.copyfile` / `shutil.move`.  
  - `create_directory`: `os.makedirs`.  
  - Hash DB: read/write of `~/.elodie/hash.json` as above.
- **Plugins**  
  - `run_all_before()` and `run_all_after()` per file ([elodie/plugins/plugins.py](elodie/plugins/plugins.py)); behavior is plugin-dependent (e.g. Google Photos upload), may have their own API or rate limits.

---

## Limitations for processing multiple files at a time

1. **ExifTool is a single shared process**
  - Implemented as a **Singleton** ([elodie/external/pyexiftool.py](elodie/external/pyexiftool.py) line 173).  
  - One long-lived subprocess; all calls go through `execute()` / `execute_json()`: write to stdin, read from stdout until sentinel. Only one call can be in flight at a time.  
  - **Implication**: If you run N workers in parallel and they all call ExifTool, you must either (a) **serialize** ExifTool access (e.g. a lock), which keeps ExifTool as a bottleneck, or (b) use **one ExifTool instance per worker** (e.g. each worker starts its own subprocess in a context manager). Option (b) is the way to get real parallelism for metadata read/write.
2. **Hash DB is shared mutable state**
  - Single file `~/.elodie/hash.json`; every file completion does `add_hash` + **full file write**.  
  - **Implication**: Multiple workers cannot safely each call `Db()`, `add_hash()`, `update_hash_db()` independently (races, lost updates, duplicate reads/writes). You need either: a **shared in-memory DB** plus a **single writer** (e.g. batch updates and write once per batch or at end), or a **lock** around DB load/add/write so only one thread/process updates at a time.  
  - Additionally, the current “load full DB twice per file + write full DB once per file” is expensive even in the serial case; batching or a single shared Db instance with periodic flush would help regardless of parallelism.
3. **Plugins**
  - Before/after hooks are synchronous and may assume single-threaded or single-file-at-a-time behavior; some plugins (e.g. upload) may have rate limits or non-thread-safe code.  
  - **Implication**: Plugin calls may need to stay on a single thread or be guarded (e.g. run in a dedicated step or behind a lock), or documented as “must be thread-safe if parallel import is enabled.”
4. **Ordering and logging**
  - Current code appends to `Result` in loop order and writes once at the end. With parallel workers, you’d need thread-safe result collection (e.g. a queue or locked list) and a clear policy for output order (e.g. by path or by completion order).

---

## Is the serial `for` loop a bottleneck?

**Yes, in the sense that:**

- Work is strictly sequential, so there is no overlap of I/O and CPU across files (e.g. while one file is being checksummed, ExifTool is idle; while ExifTool is reading metadata, the disk could be busy with another file’s copy).
- The **hash DB** design is a major cost: 2 full reads + 1 full write of `hash.json` per file. As the library grows, that file gets larger and the per-file cost increases even in the serial loop.
- ExifTool is used once for read and once for write per file; with a single process that is inherently serial and can become a bottleneck on large runs.

**So:**

- The **serial loop is a bottleneck** in that parallelizing (with a limit) can improve throughput by overlapping work and, if you use multiple ExifTool processes, by doing metadata I/O for several files at once.
- There are **hard constraints** that must be handled for correctness and performance:
  - **ExifTool**: either one process with locked access (limited gain) or one process per worker (real gain).
  - **Hash DB**: shared state and heavy per-file writes; needs a coordinated design (shared Db, batched or end-of-run writes, or locking).
  - **Plugins**: assume single-file-at-a-time unless documented/updated otherwise.

---

## Recommended direction (for a later implementation plan)

1. **Reduce hash DB I/O first (high impact, no parallelism required)**
  - Use a **single** `Db()` instance (or equivalent) for the whole import run: load once, keep in memory, and call `update_hash_db()` only at the end (or in batches) instead of after every file.  
  - Remove the second full load in `process_file` by reusing the same Db (or passing it in). This alone should noticeably speed up large imports and is a prerequisite for safe parallel updates.
2. **Add bounded parallelism**
  - Process files with a worker pool (e.g. `concurrent.futures.ThreadPoolExecutor` or `ProcessPoolExecutor`) with a max workers cap (e.g. 4–8).  
  - **ExifTool**: give each worker its own ExifTool context (e.g. each worker runs inside a `with ExifTool() as et:` or equivalent so each has a separate subprocess). That avoids a single-process bottleneck and avoids needing a global lock for metadata.  
  - **Hash DB**: keep a single logical “writer”: e.g. main process holds the Db, workers return (checksum, dest_path) and the main process (or a dedicated thread) does `add_hash` and a single batched/flush at the end (or every N files).  
  - **Plugins**: keep before/after on the worker that processes that file, or run them in a serial phase; document that plugins may be called from multiple workers if you allow that.
3. **Optional: batch ExifTool usage**
  - The library already has `get_metadata_batch(filenames)` and `set_tags_batch(tags, filenames)`. You could process files in chunks: read metadata for a chunk in one call, compute destinations, then write tags for the chunk in one call. That reduces ExifTool round-trips without introducing threads/processes, and can be combined with (1) and (2).
4. **Scoping**
  - Apply concurrency only to the **import** (and optionally **update**) command loops; leave **verify** and **generate-db** as-is unless you want to parallelize them separately with similar care for Db and any shared state.

---

## Summary


| Factor               | Role                                     | Limitation for parallelism                                                                        |
| -------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------- |
| ExifTool             | Singleton; one read + one write per file | Single process = serial metadata I/O; use one process per worker or lock (lock keeps bottleneck). |
| Hash DB              | 2 reads + 1 full write per file          | Shared mutable file; need single writer + batching or lock; also fix redundant loads.             |
| Disk (copy/checksum) | Per-file read/copy                       | Can benefit from parallel workers if ExifTool and DB are fixed.                                   |
| Plugins              | Before/after per file                    | May assume single-threaded; document or serialize.                                                |


The serial `for` loop is a bottleneck; processing multiple files at a time (up to a limit) can speed things up provided you address the ExifTool and hash DB limitations above. The highest-impact, lowest-risk first step is to fix the hash DB usage (single load, batched or end-of-run write), then add a worker pool with per-worker ExifTool instances and a single writer for the hash DB.
