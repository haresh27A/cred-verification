import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

class VercelPathFixer:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        raw_path = (
            environ.get('HTTP_X_MATCHED_PATH') or
            environ.get('HTTP_X_FORWARDED_URI') or
            environ.get('REQUEST_URI') or
            environ.get('PATH_INFO', '')
        )
        if raw_path:
            path_only = raw_path.split('?')[0]
            if path_only and path_only not in ('/api/index.py', '/api/index'):
                environ['PATH_INFO'] = path_only

        return self.app(environ, start_response)

app.wsgi_app = VercelPathFixer(app.wsgi_app)
