import uvicorn

from .config import settings


def run() -> None:
    uvicorn.run(
        "monitor.main:app",
        host=settings.bind,
        port=settings.port,
        log_level="info",
        proxy_headers=False,
    )


if __name__ == "__main__":
    run()
