-- What this file does: runs once when the database container is created.
-- It adds a separate database for the integration tests, so tests never touch your data.
CREATE DATABASE heatbeat_test OWNER heatbeat;
