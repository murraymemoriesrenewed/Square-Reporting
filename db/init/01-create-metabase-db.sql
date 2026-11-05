-- Runs once, the first time the Postgres container starts with an empty volume.
-- Metabase stores its own settings, users and saved questions in this database.
CREATE DATABASE metabase_app;
