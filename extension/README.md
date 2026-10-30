# Antfly WASM extension investigation

**There is no `hyde_search` extension here.** The Antfly checkout at
`/home/user/Desktop/antfly` does not currently provide the host operations
needed to implement the requested server-side HyDE pipeline. Registering a
handler that pretends otherwise would produce incorrect results. The application
approach does not depend on this extension interface.

## Concrete blockers in the checked-out server

Paths below are relative to `/home/user/Desktop/antfly`:

* `zig/pkg/antfly/src/extensions/wasmtime_runtime.zig:35-40,694-750` defines
  only component host functions `db.query`, `db.write`, and `ai.embed` (plus
  WASI preview 2). There is no generation or generic HTTP host function.
  The pasted `antfly.host_ai_generate`, `host_db_search`, and `host_log` are
  not provided. A component therefore cannot generate the hypothetical
  passage using the advertised host API.
* `zig/pkg/antfly/src/api/protocol_adapters.zig:1279-1300,1396-1408` shows
  that `db.query` does not forward a search body. It parses an object, reads
  only `limit` (clamped to 1–100), and constructs a `match_all` query. A
  lexical query, embedding query, fusion, or reranker supplied by a guest is
  ignored. This independently prevents the requested hybrid retrieval even if
  a passage were supplied externally.
* `zig/pkg/antfly/src/api/protocol_adapters.zig:1325-1337` shows `ai.embed`
  ignores its model argument and returns eight deterministic floats derived
  from Wyhash of the text. It is not a model embedding and cannot be used as
  a substitute for the index's dense embedding model.

These are source observations about this checkout, not a claim that all
Antfly releases behave identically. To enable the requested native tool,
Antfly would need a real, authorized generation capability and a host query
contract that preserves the required search request, with real model-backed
embedding when the guest requests one. It would also need any corresponding
WIT, runtime, permission and integration-test changes.

## Actual extension interfaces

`zig/e2e/fixtures/extensions/records/wit/antfly-extension.wit:1-25` specifies
the component contract: `db.query(table, query-json)` and
`db.write(table, writes-json)` return `result<string,string>`;
`ai.embed(model,text)` returns `result<list<f32>,string>`; and `call-tool`
accepts the tool name and request JSON, returning a `tool-result` record whose
`content-json` field contains JSON. The runtime registers versioned and
unversioned component interfaces (`wasmtime_runtime.zig:694-750`), invokes the
configured entrypoint (default `call-tool`) with those two strings, and reads
the record (`wasmtime_runtime.zig:642-691,984-999`;
`protocol_adapters.zig:1063-1067`). Component stores receive fuel and memory
limits (`wasmtime_runtime.zig:642-654`). DB calls require the respective
`db:read` or `db:write` grant, and embedding requires `ai:embed`;
table-scoped calls are bound to the installed table
(`protocol_adapters.zig:1279-1288,1302-1314,1325-1327,1344-1364`).

A core WASM module uses a different, narrower ABI: exported `memory`,
`antfly_extension_alloc`, `antfly_extension_call_tool`, and
`antfly_extension_free_buffer`. The call receives `(tool_ptr, tool_len,
request_ptr, request_len)` and returns an i64 with the response pointer in
its low 32 bits and length in its high 32 bits. The linker defines unknown
imports as traps, not Antfly host calls (`wasmtime_runtime.zig:575-638`).
The component fuel/memory setup is not applied in that core path. The pasted
`alloc`/`handle_mcp_tool` signature is not this ABI.

Packages are discovered at server startup from the filesystem package store;
there is no upload of a base64 WASM payload to `/extensions/v1`. A package
manifest declares its runtime and MCP member, with a handler of the form
`wasm:<runtime-name>/<tool-name>`; the member and handler tool names must
match. Installation uses `POST /extensions/v1/installed/{name}` after package
discovery. See `zig/EXTENSIONS.md:122-140,175-194,329-348` and
`protocol_adapters.zig:1027-1031,1075-1080,1211-1217`.

## Runnable exploration of the supported surface

The upstream `records` fixture is a real component exercising MCP dispatch,
`db.write`, `db.query`, and `ai.embed` through a separate temporary standalone
server. It is **not HyDE**, and its search is not evidence of semantic or
hybrid search: the server host query is `match_all`, and the fixture's own
search code catches host-query failures and can return an empty list
(`zig/e2e/fixtures/extensions/records/src/lib.rs:59-83`). The end-to-end test
asserts an actual inserted record is returned and that embedding has eight
values (`zig/e2e/antfly/test_extensions.py:521-579`).

Prerequisites: Rust with `wasm32-wasip2`, Cargo, `uv`, a built unified Zig
Antfly binary named `antfly` matching this checkout, and the **full**
Wasmtime C API shared library matching the checkout's CI version (not the
minimal library). The upstream test starts and stops its own server and uses
its fixture package store; it does not install anything on the server at
port 18080. From a shell:

```sh
cd /home/user/Desktop/antfly
rustup target add wasm32-wasip2
cargo build --locked --manifest-path zig/e2e/fixtures/extensions/records/Cargo.toml --release --target wasm32-wasip2
cd zig
ANTFLY_WASMTIME_LIB=/absolute/path/to/libwasmtime.so \
ANTFLY_BIN=/absolute/path/to/antfly \
uv run --project e2e/antfly pytest e2e/antfly/test_extensions.py::test_extension_records_wasm_runtime_required -q -rs
```

This builds the fixture artifact in the Antfly checkout; it requires write
access there. Check the test result: a skipped test is not evidence that the
runtime worked. The test and build were **not run** during this investigation.
See `zig/e2e/antfly/README.md:208-232` for the upstream full-suite command and
artifact requirements.

## Running server observation

A read-only `GET http://127.0.0.1:18080/extensions/v1/packages` returned HTTP
200 with `[]`; `GET /extensions/v1/installed` likewise returned HTTP 200 with
`[]`. A GET of `/mcp/v1` returned HTTP 400 `missing MCP session`. These responses
show that those routes exist and no packages or extensions were listed at the
time of inspection; they do not establish that this container runs the same
revision or has a usable Wasmtime library. No package was installed or tool
invoked on it. Recheck without mutation with:

```sh
curl -i http://127.0.0.1:18080/extensions/v1/packages
curl -i http://127.0.0.1:18080/extensions/v1/installed
```
