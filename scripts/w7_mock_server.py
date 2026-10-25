"""Demo-only fault sequence adapter; never imported by production API/MockCorp."""

import json
import os

from apps.mockcorp.main import create_app

app = create_app()
faults = json.loads(os.getenv("W7_DEMO_FAULTS", "{}"))


@app.middleware("http")
async def inject_sequence(request, call_next):
    sequence = faults.get(request.url.path, [])
    if request.method == "POST" and sequence:
        fault = sequence.pop(0)
        if fault:
            request.scope["query_string"] = ("fault=" + fault).encode()
    return await call_next(request)
