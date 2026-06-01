FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY web /app/web
COPY README.md /app/README.md

ENV HOST=0.0.0.0
ENV PORT=8000
ENV BROWSER_HEADLESS=true
ENV BROWSER_NO_SANDBOX=true

CMD ["python", "-m", "app.main"]
