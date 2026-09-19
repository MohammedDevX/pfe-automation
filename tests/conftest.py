import os
import pytest

# Ensure pytest tests run against an isolated test database, preserving pfe_jobs.db
os.environ['DATABASE_URL'] = 'sqlite:///./test_pfe_jobs.db'
