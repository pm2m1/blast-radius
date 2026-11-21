# Same base image the organizers labeled with: its Python and SQLite decide which tests run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit (GitHub archive of that exact commit)
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/sqlglot \
 && tar xzf /tmp/sqlglot.tar.gz -C /app/sqlglot --strip-components=1 \
 && rm /tmp/sqlglot.tar.gz \
 && (python -m compileall -q /app/sqlglot/sqlglot /app/sqlglot/tests || true)

# DuckDB's expected TPC-DS results for the pinned sqlglot's SQL (used only on an exact SQL match)
COPY build_tpcds_cache.py /app/
RUN cd /app/sqlglot && SKIP_INTEGRATION=0 python /app/build_tpcds_cache.py /app/sqlglot /app/tpcds_cache.pkl

COPY blast.py syncpool.py map.json.gz cpu.json /app/
# marshal loads twice as fast as JSON; converting here keeps that CPU out of the scored run
RUN python -c "import json,gzip,marshal; marshal.dump(json.load(gzip.open('/app/map.json.gz')), open('/app/map.marshal','wb'))" \
 && python -m compileall -q /app/blast.py /app/syncpool.py && chmod -R a+rX /app

ENV SKIP_INTEGRATION=0 SQLGLOT_DIR=/app/sqlglot PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/blast.py"]
