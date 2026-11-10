FROM python:3.12-slim
WORKDIR /app
COPY server.py rag.py warm_cache.py ./
COPY static ./static
COPY demo ./demo
ENV MI_PUBLIC=1 HOST=0.0.0.0 NO_BROWSER=1 PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
