"""Run the standalone NexaCard OTP HTTP service."""

import uvicorn

from nexacard_otp.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "nexacard_otp.app:app",
        host=settings.service_host,
        port=settings.service_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
