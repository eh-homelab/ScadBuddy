# A Bambuddy that fails every hot-path call and lists it (#3818 tier A, Task 12);
# recorder.py's docstring says what it answers.
#
#   docker build -t scadbuddy-bambuddy-recorder:dev e2e-stack/bambuddy-recorder
#   docker run --rm -p 8000:8000 scadbuddy-bambuddy-recorder:dev
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

RUN useradd --system --uid 10001 --home-dir /app recorder

COPY recorder.py /app/
COPY answers/ /app/answers/

USER 10001
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=2s --timeout=2s --retries=15 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/__recorded', timeout=1)"]
ENTRYPOINT ["python", "/app/recorder.py"]
