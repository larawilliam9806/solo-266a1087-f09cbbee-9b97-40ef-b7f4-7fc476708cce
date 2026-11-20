"""FastAPI application."""

from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .db import db_path_from_env
from .errors import ApiError
from .models import (
    QUARTER_PATTERN,
    DetailSubmission,
    MaskSubmission,
    PublishRequest,
    SuggestionRequest,
    TokenRequest,
    TotalsRegistration,
)
from .service import BulletinService

def create_app(db_path: str | None = None) -> FastAPI:
    service = BulletinService(db_path or db_path_from_env())
    bootstrap_token = service.ensure_editor_token()

    app = FastAPI(
        title="Relief Household Bulletin API",
        version=__version__,
        description=(
            "Per-quarter cross-tabulation of relief households by district and "
            "industry. Published row/column totals are fixed at registration; "
            "cell values are disclosed only where masking permits."
        ),
    )
    app.state.service = service
    app.state.bootstrap_token = bootstrap_token

    if bootstrap_token:
        # Printed once on an empty database; only the hash is stored.
        print(f"[bulletin] bootstrap editor token: {bootstrap_token}",
              flush=True)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.as_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request,
                                exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"location": ".".join(str(part) for part in err["loc"]),
             "message": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request",
                               "message": "the request body or parameters are "
                                          "invalid",
                               "details": {"errors": errors}}},
        )

    def quarter_param(quarter: str) -> str:
        if not QUARTER_PATTERN.fullmatch(quarter):
            raise ApiError(422, "invalid_quarter",
                           "quarter must look like 2024Q1",
                           {"quarter": quarter})
        return quarter

    Quarter = Annotated[str, Depends(quarter_param)]

    def editor_id(
        authorization: Annotated[str | None, Header()] = None,
    ) -> int:
        if not authorization:
            raise ApiError(401, "token_required",
                           "an editor token is required",
                           {"header": "Authorization: Bearer <token>"})
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "token_malformed",
                           "Authorization header must be 'Bearer <token>'")
        return service.auth(token.strip())

    Editor = Annotated[int, Depends(editor_id)]

    # ------------------------------------------------------------- public API

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/bulletins")
    def list_bulletins() -> dict[str, Any]:
        return {"bulletins": service.list_public()}

    @app.get("/api/v1/bulletins/{quarter}")
    def get_bulletin(quarter: Quarter) -> dict[str, Any]:
        return service.get_public(quarter)

    # ----------------------------------------------------------------- editor

    @app.post("/api/v1/editor/tokens")
    def create_token(body: TokenRequest, editor: Editor) -> dict[str, Any]:
        token_id, token = service.create_token(body.label)
        return {"id": token_id, "label": body.label, "token": token}

    @app.get("/api/v1/editor/quarters/{quarter}")
    def editor_quarter(quarter: Quarter, editor: Editor) -> dict[str, Any]:
        return service.get_admin_state(quarter)

    @app.put("/api/v1/editor/quarters/{quarter}/totals")
    def register_totals(quarter: Quarter, editor: Editor,
                        body: TotalsRegistration = Body(...)) -> dict[str, Any]:
        return service.register_totals(quarter, body, editor)

    @app.post("/api/v1/editor/quarters/{quarter}/detail")
    def submit_detail(quarter: Quarter, editor: Editor,
                      body: DetailSubmission = Body(...)) -> dict[str, Any]:
        return service.submit_detail(quarter, body, editor)

    @app.put("/api/v1/editor/quarters/{quarter}/mask")
    def put_mask(quarter: Quarter, editor: Editor,
                 body: MaskSubmission = Body(...)) -> dict[str, Any]:
        return service.put_mask(quarter, body, editor)

    @app.post("/api/v1/editor/quarters/{quarter}/suppression-advice")
    def suppression_advice(quarter: Quarter, editor: Editor,
                           body: SuggestionRequest = Body(
                               default_factory=SuggestionRequest)
                           ) -> dict[str, Any]:
        return service.suggest(quarter, body.detail_version, body.cells)

    @app.post("/api/v1/editor/quarters/{quarter}/publish")
    def publish(quarter: Quarter, editor: Editor,
                body: PublishRequest = Body(default_factory=PublishRequest)
                ) -> dict[str, Any]:
        return service.publish(quarter, body, editor)

    return app


def __getattr__(name: str):
    """Build the ASGI app lazily so merely importing this module (tests,
    tooling) does not create a database."""
    if name == "app":
        value = create_app()
        globals()["app"] = value
        return value
    raise AttributeError(name)
