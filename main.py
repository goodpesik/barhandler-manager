import uvicorn
from src.server import create_app
from src.config import load_config

config = load_config()
app = create_app(config)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=config["server"]["port"])
