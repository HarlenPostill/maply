-- Separate database for the automated test suite so tests never touch project data.
CREATE DATABASE maply_test OWNER maply;
\connect maply_test
CREATE EXTENSION IF NOT EXISTS postgis;
\connect maply
CREATE EXTENSION IF NOT EXISTS postgis;
