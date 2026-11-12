# Contract

The HTTP contract between the CompassStudio server and client (decision D2):
an OpenAPI 3.1 spec, versioned binary schemas, and conformance tests.

Status: skeleton. Not frozen until spikes S1 to S6 are done (decision log
section 6).

- `openapi.yaml`: the JSON API, path-versioned under `/api/v1`.
- `schemas/`: binary payload schemas (Arrow IPC, raw buffers), versioned
  separately from the JSON API.
