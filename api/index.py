import os
import sys

# Add root directory to python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

class VercelPathFixer:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        # Extract original requested path from Vercel headers
        original_path = (
            environ.get('HTTP_X_MATCHED_PATH') or
            environ.get('HTTP_X_INVOKE_PATH') or
            environ.get('HTTP_X_FORWARDED_URI') or
            environ.get('RAW_URI') or
            environ.get('PATH_INFO', '')
        )
        # Strip query strings if present in raw URI
        if original_path:
            original_path = original_path.split('?')[0]
            if not original_path.endswith('api/index.py') and not original_path.endswith('api/index'):
                environ['PATH_INFO'] = original_path

        return self.app(environ, start_response)

app.wsgi_app = VercelPathFixer(app.wsgi_app)
