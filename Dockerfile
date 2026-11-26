# BBQ — CyberQ dashboard container (task 6)
#
# Python 3.12, FastAPI + uvicorn + httpx only. The app runs from /srv/bbq
# (PYTHONPATH) so the static/ UI ships next to app/ in the image.

FROM python:3.12-slim

WORKDIR /srv/bbq

COPY pyproject.toml README.md DESIGN.md ./
COPY app ./app
COPY static ./static

RUN pip install --no-cache-dir .

ENV PYTHONPATH=/srv/bbq \
    DB_PATH=/data/bbq.db \
    POLL_INTERVAL=5

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
