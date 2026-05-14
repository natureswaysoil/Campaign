"""Cloud Run entrypoint wrapper for the Amazon PPC Optimizer.

The main FastAPI application lives in app.py. Its dashboard route renders
`templates/dashboard.html` through Jinja2, but the dashboard is a static
HTML + JavaScript app. Some JavaScript object syntax can be interpreted by
Jinja before the browser sees it, causing errors such as:

    TypeError: unhashable type: 'dict'

This wrapper imports the main app, removes the Jinja-rendered root route,
and replaces it with a plain static HTML response. All API routes from app.py
remain available.
"""
from pathlib import Path

from fastapi.responses import HTMLResponse

from app import app

BASE_DIR = Path(__file__).parent.absolute()
DASHBOARD_PATH = BASE_DIR / "templates" / "dashboard.html"


def _is_root_get_route(route) -> bool:
    return (
        getattr(route, "path", None) == "/"
        and "GET" in set(getattr(route, "methods", set()) or set())
    )


# Remove the original Jinja dashboard route from app.py.
app.router.routes = [route for route in app.router.routes if not _is_root_get_route(route)]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_static():
    try:
        return HTMLResponse(
            DASHBOARD_PATH.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f"""
            <h2>Amazon PPC Optimizer Dashboard</h2>
            <p>Service is running.</p>
            <p style=\"color: red; margin: 20px 0;\">
                <strong>Dashboard File Error:</strong> {type(exc).__name__}<br>
                {exc}
            </p>
            <p>Base directory: {BASE_DIR}</p>
            <p>Dashboard path: {DASHBOARD_PATH}</p>
            """,
            status_code=500,
        )
