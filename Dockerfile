FROM python:3.12-slim

# App files only; real CSVs/.env stay host-side via mounted volumes (README).
COPY check_for_craiglist_deals.py config.py acquire.py parse.py notify.py \
     selection.py clock.py deliver.py state.py /app/
COPY requirements.txt requirements-dev.txt constraints.txt /app/

RUN useradd -M -u 1000 -s /usr/sbin/nologin cfd && \
    mkdir -p /svc/data /svc/logs && chown -R cfd /svc && \
    python -m venv /app/.venv && \
    /app/.venv/bin/pip install -q --no-cache-dir -r /app/requirements-dev.txt \
      -c /app/constraints.txt && \
    /app/.venv/bin/pip check

USER 1000
WORKDIR /app

# Mount: -v /host/state:/svc/data -v /host/logs:/svc/logs -v /host/configs:/app/configs
# (mount dirs must be writable by uid 1000; app refuses otherwise with exit 2)
CMD ["/app/.venv/bin/python", "/app/check_for_craiglist_deals.py"]
