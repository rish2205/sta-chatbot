from copy import deepcopy

import uvicorn

if __name__ == "__main__":
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["loggers"]["nia"] = {
        "handlers": ["default"], "level": "INFO", "propagate": False,
    }
    uvicorn.run("server:app", port=8000, log_config=log_config)
