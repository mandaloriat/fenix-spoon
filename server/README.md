# fenixspoon (server)

The Python server package of [Fenix Spoon](https://github.com/mandaloriat/fenix-spoon): a FastAPI
application exposing pluggable finite-element solvers (FEniCSx or the built-in NumPy mock) behind
a JSON wire protocol with WebSocket progress streaming.

```bash
pip install -e ".[dev]"
uvicorn fenixspoon.main:app --reload
pytest
```

See the repository root [README](../README.md) and [docs](../docs/) for the full picture.
