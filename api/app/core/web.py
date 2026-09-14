"""Static UI hosting and compatibility with the development proxy prefix."""

from starlette.staticfiles import StaticFiles


class ApiPrefixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope["path"].startswith("/mysterious/"):
            scope = dict(scope)
            scope["path"] = scope["path"][len("/mysterious"):]
            scope.pop("raw_path", None)
        await self.app(scope, receive, send)


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in (".", "", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response
