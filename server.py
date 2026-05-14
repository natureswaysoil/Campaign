"""Cloud Run entrypoint wrapper for the Amazon PPC Optimizer.

The full dashboard/API application lives in optimize_campaigns.py. Its root
route serves the dashboard HTML directly, but this wrapper keeps Cloud Run on a
stable entrypoint and makes sure the dashboard is served as plain static HTML.

Do not import app.py here; app.py is a smaller alternate app and does not expose
all dashboard endpoints such as /api/dashboard-data.
"""
from pathlib import Path

from fastapi.responses import HTMLResponse

from optimize_campaigns import app

BASE_DIR = Path(__file__).parent.absolute()
DASHBOARD_PATH = BASE_DIR / "templates" / "dashboard.html"


def _is_root_get_route(route) -> bool:
    return (
        getattr(route, "path", None) == "/"
        and "GET" in set(getattr(route, "methods", set()) or set())
    )


# Remove the original root dashboard route and replace it with a static response.
# All API routes from optimize_campaigns.py remain available.
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
