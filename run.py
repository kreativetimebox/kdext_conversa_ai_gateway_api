"""Local development server runner."""

import uvicorn
from app.config import get_settings

# main 
def main() -> None:
    """Run the FastAPI Voice Gateway app with Uvicorn."""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "local",
    )


if __name__ == "__main__":
    main()
