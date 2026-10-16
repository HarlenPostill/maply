from __future__ import annotations

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString

from app.geo.planning import PlanParams
from app.models import CapturePlan, Job, Project, Route
from app.schemas import JobCreate, ProjectConfig
from app.scheduler import jobs as jobsvc
from app.services import plans as plansvc

# A short, real stretch of George St (CBD), drawn northbound.
GEORGE_ST = [(151.20668, -33.87501), (151.20690, -33.87360)]


def make_project(s, name: str = "Test project", **cfg) -> Project:
    import json

    from shapely.geometry import shape

    from app.config import get_settings

    gj = json.loads(get_settings().scope_geojson_path.read_text())
    from shapely.geometry import MultiPolygon

    scope = MultiPolygon([shape(f["geometry"]) for f in gj["features"]])
    p = Project(name=name, description="", config=ProjectConfig(**cfg).model_dump(mode="json"), scope_geom=from_shape(scope, srid=4326))
    s.add(p)
    s.flush()
    return p


def make_plan(s, project: Project, coords=GEORGE_ST, approve: bool = True, **params) -> CapturePlan:
    defaults = {"sampling_interval_m": 30.0, "views_per_side": 1, "include_crossing_approaches": False, "viewport_width": 800, "viewport_height": 500}
    defaults.update(params)
    r = Route(project_id=project.id, name="George St", kind="route", geom=from_shape(LineString(coords), srid=4326), properties={})
    s.add(r)
    s.flush()
    plan = plansvc.create_plan(s, project, [r.id], PlanParams(**defaults), [])
    if approve:
        plan.status = "approved"
    s.flush()
    return plan


def make_job(s, plan: CapturePlan, start: bool = True, **kw) -> Job:
    req = JobCreate(**{"target": "synthetic", "method": "deterministic_browser", "requests_per_minute": 600, **kw})
    job = jobsvc.create_job(s, plan, req)
    s.flush()
    if start:
        jobsvc.start_job(s, job)
    s.commit()
    return job
