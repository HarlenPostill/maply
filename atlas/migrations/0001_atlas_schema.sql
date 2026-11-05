-- Maply atlas: the production street-accessibility dataset.
--
-- Facts only: no photos, model confidence, evidence text or image regions.
-- Survey data (from the Maply tagger) and community data (Petal Path players)
-- share one observation model, so every derived view treats them identically.
--
--   segments ─┬─ survey_points ── observations ── observation_features
--             ├─ segment_sides   (materialized: per-side footpath summary + grade)
--             └─ routing_edges ── routing_nodes
--                routing_network (materialized: pgRouting-ready wheelchair costs)
--   verification_targets (view: where the dataset most needs a fresh look)

CREATE EXTENSION IF NOT EXISTS postgis;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgrouting') THEN
    CREATE EXTENSION IF NOT EXISTS pgrouting;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS atlas;
COMMENT ON SCHEMA atlas IS 'Street accessibility atlas: surveyed segments, footpath observations, routing graph.';

-- ---------------------------------------------------------------------------
-- Provenance
-- ---------------------------------------------------------------------------

CREATE TABLE atlas.releases (
    id                 integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source             text NOT NULL CHECK (source IN ('maply_survey')),
    schema_version     text NOT NULL,
    source_fingerprint text NOT NULL,
    stats              jsonb NOT NULL,
    published_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE atlas.releases IS 'Each publish of tagger survey data. Survey rows always belong to the latest release.';

CREATE TABLE atlas.coverage_areas (
    id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE,
    geom geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX coverage_areas_geom ON atlas.coverage_areas USING gist (geom);
COMMENT ON TABLE atlas.coverage_areas IS 'Areas the survey set out to cover (the playable / routable region).';

-- ---------------------------------------------------------------------------
-- Streets
-- ---------------------------------------------------------------------------

CREATE TABLE atlas.segments (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    maply_key   text NOT NULL UNIQUE,
    street_name text,
    highway     text NOT NULL,
    length_m    real NOT NULL CHECK (length_m > 0),
    bearing_deg real NOT NULL,
    osm         jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(osm) = 'object'),
    geom        geometry(LineString, 4326) NOT NULL
);
CREATE INDEX segments_geom ON atlas.segments USING gist (geom);
COMMENT ON TABLE atlas.segments IS 'Surveyed street centreline pieces. Left/right are relative to the line direction.';
COMMENT ON COLUMN atlas.segments.maply_key IS 'Stable geometry-derived key from the tagger; ids are preserved across publishes by this key.';
COMMENT ON COLUMN atlas.segments.osm IS 'OpenStreetMap tags relevant to walking: sidewalk*, surface, lit, maxspeed, width, kerb, lanes, oneway.';

CREATE TABLE atlas.survey_points (
    id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    maply_shot_id uuid NOT NULL UNIQUE,
    code          text NOT NULL,
    segment_id    integer NOT NULL REFERENCES atlas.segments ON DELETE CASCADE,
    side          text NOT NULL CHECK (side IN ('left', 'right')),
    facing        text,
    status        text NOT NULL CHECK (status IN ('observed', 'pending', 'no_imagery', 'blocked', 'skipped')),
    heading       real NOT NULL,
    geom          geometry(Point, 4326) NOT NULL,
    release_id    integer NOT NULL REFERENCES atlas.releases
);
CREATE INDEX survey_points_geom ON atlas.survey_points USING gist (geom);
CREATE INDEX survey_points_segment ON atlas.survey_points (segment_id, side);
COMMENT ON TABLE atlas.survey_points IS 'Every planned survey location, including the ones with no usable imagery.';
COMMENT ON COLUMN atlas.survey_points.facing IS 'Compass side of the street the footpath is on, e.g. west.';
COMMENT ON COLUMN atlas.survey_points.status IS 'observed, or why there is no observation: pending (not yet captured/tagged), no_imagery, blocked, skipped.';

-- ---------------------------------------------------------------------------
-- Observations
-- ---------------------------------------------------------------------------

CREATE TABLE atlas.observations (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source             text NOT NULL CHECK (source IN ('maply_survey', 'community')),
    survey_point_id    integer REFERENCES atlas.survey_points ON DELETE SET NULL,
    segment_id         integer NOT NULL REFERENCES atlas.segments ON DELETE RESTRICT,
    side               text NOT NULL CHECK (side IN ('left', 'right')),
    geom               geometry(Point, 4326) NOT NULL,
    observed_on        date,
    footpath           text CHECK (footpath IN ('present', 'absent')),
    needs_verification boolean NOT NULL DEFAULT false,
    release_id         integer REFERENCES atlas.releases,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((source = 'maply_survey') = (release_id IS NOT NULL))
);
CREATE INDEX observations_geom ON atlas.observations USING gist (geom);
CREATE INDEX observations_segment ON atlas.observations (segment_id, side);
CREATE INDEX observations_point ON atlas.observations (survey_point_id, observed_on DESC);
CREATE INDEX observations_source ON atlas.observations (source);
COMMENT ON TABLE atlas.observations IS 'One look at one footpath: a survey photo or a player report.';
COMMENT ON COLUMN atlas.observations.observed_on IS 'When the footpath looked like this: Street View imagery month (day 1) for survey data, visit date for community data.';
COMMENT ON COLUMN atlas.observations.footpath IS 'present or absent; NULL when it could not be determined.';
COMMENT ON COLUMN atlas.observations.needs_verification IS 'The observation was flagged as uncertain and should be re-checked on the ground.';

CREATE TABLE atlas.observation_features (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES atlas.observations ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('footpath_section', 'driveway', 'kerb_ramp', 'crossing', 'hazard')),
    attrs          jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attrs) = 'object')
);
CREATE INDEX observation_features_observation ON atlas.observation_features (observation_id);
CREATE INDEX observation_features_kind ON atlas.observation_features (kind);
CREATE INDEX observation_features_attrs ON atlas.observation_features USING gin (attrs jsonb_path_ops);
COMMENT ON TABLE atlas.observation_features IS 'Footpath sections, driveways, kerb ramps, crossings and hazards seen in an observation.';
COMMENT ON COLUMN atlas.observation_features.attrs IS 'Known tag values only, e.g. {"surface":"concrete","condition":"no_visible_defect"}. Vocabulary: maply-accessibility/1.';

-- ---------------------------------------------------------------------------
-- Derived signals (one definition for survey and community data)
-- ---------------------------------------------------------------------------

-- Per observation: NULL = not observed, true = concern seen, false = observed without concern.
CREATE VIEW atlas.observation_signals AS
WITH f AS (
    SELECT observation_id, kind, attrs,
           coalesce(attrs->>'route_relation', '') <> 'beside_route'
             AND coalesce(attrs->>'affected_width', '') <> 'outside_route' AS on_route
    FROM atlas.observation_features
), per_obs AS (
    SELECT observation_id,
        bool_or(attrs->>'clear_passage' IN ('narrowed', 'blocked'))
            FILTER (WHERE on_route AND attrs ? 'clear_passage') AS passage,
        bool_or(attrs->>'condition' = 'visible_defect'
                OR attrs->>'texture' IN ('rough', 'very_rough')
                OR attrs->>'surface' IN ('loose_gravel', 'earth', 'sand', 'grass', 'rounded_cobblestones'))
            FILTER (WHERE on_route AND (attrs ?| ARRAY['condition', 'texture', 'surface'])) AS surface,
        bool_or(attrs->>'cross_slope' IN ('moderate', 'steep')
                OR attrs->>'longitudinal_slope' IN ('moderate', 'steep')
                OR attrs->>'slope' IN ('moderate', 'steep'))
            FILTER (WHERE on_route AND (attrs ?| ARRAY['cross_slope', 'longitudinal_slope', 'slope'])) AS slope,
        bool_or(attrs->>'presence' = 'absent'
                OR attrs->>'transition_type' IN ('unmodified_kerb', 'steps')
                OR attrs->>'bottom_join' IN ('raised_lip', 'step', 'open_gap', 'channel'))
            FILTER (WHERE on_route AND kind IN ('crossing', 'kerb_ramp')
                    AND (attrs ?| ARRAY['presence', 'transition_type', 'bottom_join'])) AS crossing,
        bool_or(kind = 'hazard' AND attrs ? 'type'
                AND attrs->>'route_relation' IN ('on_route', 'partially_encroaching')) AS hazard,
        -- Hard stops for a wheelchair: blocked, steep, steps, path ends, or an
        -- unavoidable full-width hazard.
        bool_or(on_route AND (
                attrs->>'clear_passage' = 'blocked'
             OR 'steep' IN (attrs->>'cross_slope', attrs->>'longitudinal_slope', attrs->>'slope')
             OR attrs->>'interruption' IN ('steps', 'barrier', 'ends_in_road')
             OR attrs->>'continuity' = 'ends'
             OR attrs->>'transition_type' = 'steps'
             OR attrs->>'entry_join' IN ('step', 'open_gap')
             OR attrs->>'exit_join' IN ('step', 'open_gap')
             OR (kind = 'hazard' AND attrs->>'affected_width' = 'full_width' AND attrs->>'bypass' = 'not_visible'))) AS barrier,
        bool_or(attrs->>'condition' = 'visible_defect'
                OR attrs->>'texture' IN ('rough', 'very_rough')
                OR attrs->>'surface' IN ('loose_gravel', 'earth', 'sand', 'grass', 'rounded_cobblestones')
                OR attrs->>'continuity' IN ('interrupted', 'ends')
                OR attrs->>'interruption' IN ('missing_surface', 'steps', 'barrier', 'construction', 'ends_in_road'))
            FILTER (WHERE kind = 'footpath_section' AND on_route AND attrs ? 'presence') AS quality_concern,
        bool_and(attrs->>'condition' = 'no_visible_defect')
            FILTER (WHERE kind = 'footpath_section' AND on_route AND attrs ? 'presence') AS quality_ok
    FROM f
    GROUP BY observation_id
)
SELECT o.id AS observation_id, o.source, o.segment_id, o.side, o.survey_point_id,
       o.observed_on, o.footpath, o.needs_verification,
       s.passage, s.surface, s.slope, s.crossing,
       CASE WHEN o.footpath = 'present' THEN coalesce(s.hazard, false) END AS hazard,
       CASE WHEN o.footpath = 'present' THEN coalesce(s.barrier, false) END AS barrier,
       CASE WHEN o.footpath <> 'present' THEN NULL
            WHEN s.quality_concern THEN true
            WHEN s.quality_ok THEN false END AS quality
FROM atlas.observations o
LEFT JOIN per_obs s ON s.observation_id = o.id;
COMMENT ON VIEW atlas.observation_signals IS 'Accessibility concerns per observation. NULL means not observed.';

-- Wheelchair suitability of one footpath side, from 0 (unusable) to 1.
-- Shares are the fraction of observations showing each concern.
CREATE FUNCTION atlas.side_score(
    footpath_share real, barrier_share real, passage_share real, slope_share real,
    surface_share real, hazard_share real, quality_share real
) RETURNS real LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN footpath_share IS NULL THEN NULL ELSE
        greatest(0, footpath_share * (1 - least(1,
              1.00 * coalesce(barrier_share, 0)
            + 0.50 * coalesce(passage_share, 0)
            + 0.50 * coalesce(slope_share, 0)
            + 0.35 * coalesce(surface_share, 0)
            + 0.25 * coalesce(hazard_share, 0)
            + 0.15 * coalesce(quality_share, 0))))::real
    END
$$;

CREATE FUNCTION atlas.side_grade(footpath_share real, score real)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN footpath_share IS NULL THEN 'unknown'
        WHEN footpath_share < 0.5 THEN 'no_footpath'
        WHEN score >= 0.8 THEN 'good'
        WHEN score >= 0.55 THEN 'fair'
        ELSE 'poor'
    END
$$;

CREATE MATERIALIZED VIEW atlas.segment_sides AS
WITH sides AS (
    SELECT segment_id, side FROM atlas.survey_points
    UNION
    SELECT segment_id, side FROM atlas.observations
), planned AS (
    SELECT segment_id, side, count(*)::integer AS planned_points
    FROM atlas.survey_points GROUP BY 1, 2
), agg AS (
    SELECT segment_id, side,
        count(*)::integer AS observations,
        count(*) FILTER (WHERE source = 'community')::integer AS community_observations,
        count(footpath)::integer AS footpath_observed,
        avg((footpath = 'present')::int)::real AS footpath_share,
        avg(barrier::int)::real AS barrier_share,
        avg(passage::int)::real AS passage_share,
        avg(slope::int)::real AS slope_share,
        avg(surface::int)::real AS surface_share,
        avg(hazard::int)::real AS hazard_share,
        avg(quality::int)::real AS quality_share,
        avg(crossing::int)::real AS crossing_share,
        count(*) FILTER (WHERE barrier)::integer AS barriers,
        count(*) FILTER (WHERE needs_verification)::integer AS needs_verification,
        min(observed_on) AS first_observed_on,
        max(observed_on) AS last_observed_on
    FROM atlas.observation_signals
    GROUP BY 1, 2
), surfaces AS (
    SELECT o.segment_id, o.side, mode() WITHIN GROUP (ORDER BY f.attrs->>'surface') AS dominant_surface
    FROM atlas.observation_features f
    JOIN atlas.observations o ON o.id = f.observation_id
    WHERE f.kind = 'footpath_section' AND f.attrs ? 'surface'
    GROUP BY 1, 2
)
SELECT s.segment_id, s.side,
       coalesce(p.planned_points, 0) AS planned_points,
       coalesce(a.observations, 0) AS observations,
       coalesce(a.community_observations, 0) AS community_observations,
       coalesce(a.footpath_observed, 0) AS footpath_observed,
       CASE WHEN a.footpath_share IS NULL THEN 'unknown'
            WHEN a.footpath_share >= 0.8 THEN 'present'
            WHEN a.footpath_share <= 0.2 THEN 'absent'
            ELSE 'partial' END AS footpath,
       a.footpath_share, a.barrier_share, a.passage_share, a.slope_share,
       a.surface_share, a.hazard_share, a.quality_share, a.crossing_share,
       coalesce(a.barriers, 0) AS barriers,
       coalesce(a.needs_verification, 0) AS needs_verification,
       u.dominant_surface,
       a.first_observed_on, a.last_observed_on,
       sc.score,
       atlas.side_grade(a.footpath_share, sc.score) AS grade,
       ST_Transform(ST_OffsetCurve(ST_Transform(g.geom, 7856),
                    CASE s.side WHEN 'left' THEN 5 ELSE -5 END), 4326) AS geom
FROM sides s
JOIN atlas.segments g ON g.id = s.segment_id
LEFT JOIN planned p USING (segment_id, side)
LEFT JOIN agg a USING (segment_id, side)
LEFT JOIN surfaces u USING (segment_id, side)
CROSS JOIN LATERAL (SELECT atlas.side_score(a.footpath_share, a.barrier_share, a.passage_share,
    a.slope_share, a.surface_share, a.hazard_share, a.quality_share) AS score) sc;
CREATE UNIQUE INDEX segment_sides_pk ON atlas.segment_sides (segment_id, side);
CREATE INDEX segment_sides_geom ON atlas.segment_sides USING gist (geom);
CREATE INDEX segment_sides_grade ON atlas.segment_sides (grade);
COMMENT ON MATERIALIZED VIEW atlas.segment_sides IS 'Per footpath side: what was seen, a 0-1 wheelchair score and grade (good, fair, poor, no_footpath, unknown). Refresh with atlas.refresh_derived().';

-- ---------------------------------------------------------------------------
-- Routing graph
-- ---------------------------------------------------------------------------

CREATE TABLE atlas.routing_nodes (
    id     integer PRIMARY KEY,
    degree smallint NOT NULL,
    geom   geometry(Point, 4326) NOT NULL
);
CREATE INDEX routing_nodes_geom ON atlas.routing_nodes USING gist (geom);

CREATE TABLE atlas.routing_edges (
    id         integer PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('street', 'connector')),
    segment_id integer REFERENCES atlas.segments ON DELETE CASCADE,
    source     integer NOT NULL REFERENCES atlas.routing_nodes,
    target     integer NOT NULL REFERENCES atlas.routing_nodes,
    start_m    real NOT NULL,
    end_m      real NOT NULL,
    length_m   real NOT NULL,
    geom       geometry(LineString, 4326) NOT NULL,
    CHECK ((kind = 'street') = (segment_id IS NOT NULL))
);
CREATE INDEX routing_edges_segment ON atlas.routing_edges (segment_id);
CREATE INDEX routing_edges_source ON atlas.routing_edges (source);
CREATE INDEX routing_edges_target ON atlas.routing_edges (target);
CREATE INDEX routing_edges_geom ON atlas.routing_edges USING gist (geom);
COMMENT ON TABLE atlas.routing_edges IS 'Segments split at junctions, plus short connectors where a surveyed street stops just short of another. Ids are rebuilt on every publish: reference segments, not edges.';

-- Splits segments wherever another segment ends on or crosses them, joins
-- endpoints within snap_m into shared nodes, and links dead ends that stop
-- within gap_m of another street with a connector edge.
CREATE FUNCTION atlas.rebuild_routing_graph(
    snap_m double precision DEFAULT 1.0, gap_m double precision DEFAULT 12.0, bridge_m double precision DEFAULT 20.0)
RETURNS TABLE (node_count integer, edge_count integer)
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM atlas.routing_edges;
    DELETE FROM atlas.routing_nodes;
    DROP TABLE IF EXISTS pg_temp._seg, pg_temp._ends, pg_temp._gaps, pg_temp._cuts, pg_temp._pieces, pg_temp._pts;

    CREATE TEMPORARY TABLE _seg ON COMMIT DROP AS
        SELECT id, ST_Transform(geom, 7856) AS g, ST_Length(ST_Transform(geom, 7856)) AS len
        FROM atlas.segments;
    CREATE INDEX ON _seg USING gist (g);
    ANALYZE _seg;

    CREATE TEMPORARY TABLE _ends ON COMMIT DROP AS
        SELECT id, ST_StartPoint(g) AS pt FROM _seg
        UNION ALL SELECT id, ST_EndPoint(g) FROM _seg;
    CREATE INDEX ON _ends USING gist (pt);
    ANALYZE _ends;

    -- Dead ends: no other segment within snap_m, but one within gap_m.
    CREATE TEMPORARY TABLE _gaps ON COMMIT DROP AS
    SELECT row_number() OVER () AS n, e.pt AS from_pt, near.id AS to_id,
           ST_LineLocatePoint(near.g, e.pt) AS to_f, ST_ClosestPoint(near.g, e.pt) AS to_pt
    FROM _ends e
    CROSS JOIN LATERAL (SELECT s.id, s.g FROM _seg s WHERE s.id <> e.id AND ST_DWithin(s.g, e.pt, gap_m)
                        ORDER BY s.g <-> e.pt LIMIT 1) near
    WHERE NOT EXISTS (SELECT 1 FROM _seg s WHERE s.id <> e.id AND ST_DWithin(s.g, e.pt, snap_m));

    CREATE TEMPORARY TABLE _cuts ON COMMIT DROP AS
    WITH raw AS (
        SELECT id, 0::float8 AS f FROM _seg
        UNION ALL SELECT id, 1::float8 FROM _seg
        UNION ALL
        SELECT s.id, ST_LineLocatePoint(s.g, e.pt)
        FROM _seg s JOIN _ends e ON e.id <> s.id AND ST_DWithin(s.g, e.pt, snap_m)
        UNION ALL
        SELECT to_id, to_f FROM _gaps
        UNION ALL
        SELECT s.id, ST_LineLocatePoint(s.g, d.geom)
        FROM _seg s
        JOIN _seg o ON o.id <> s.id AND ST_Intersects(s.g, o.g)
        CROSS JOIN LATERAL ST_Dump(ST_Intersection(s.g, o.g)) d
        WHERE GeometryType(d.geom) = 'POINT'
    )
    SELECT DISTINCT r.id, s.len, round((r.f * s.len)::numeric, 1)::float8 AS m
    FROM raw r JOIN _seg s USING (id);

    CREATE TEMPORARY TABLE _pieces ON COMMIT DROP AS
    SELECT c.id AS segment_id, c.m0, c.m1,
           ST_LineSubstring(s.g, least(1, c.m0 / s.len), least(1, c.m1 / s.len)) AS g
    FROM (SELECT id, len, m AS m0, lead(m) OVER (PARTITION BY id ORDER BY m) AS m1 FROM _cuts) c
    JOIN _seg s USING (id)
    WHERE c.m1 IS NOT NULL AND c.m1 - c.m0 >= 0.05 AND c.m0 / s.len < 1;

    -- Every edge end, street pieces (ref = segment:m0) and connectors (ref = gap number).
    CREATE TEMPORARY TABLE _pts ON COMMIT DROP AS
    SELECT ref, role, pt, ST_ClusterDBSCAN(pt, eps := snap_m, minpoints := 1) OVER () AS cluster
    FROM (SELECT segment_id || ':' || m0 AS ref, 'source' AS role, ST_StartPoint(g) AS pt FROM _pieces
          UNION ALL SELECT segment_id || ':' || m0, 'target', ST_EndPoint(g) FROM _pieces
          UNION ALL SELECT 'gap:' || n, 'source', from_pt FROM _gaps
          UNION ALL SELECT 'gap:' || n, 'target', to_pt FROM _gaps) p;

    INSERT INTO atlas.routing_nodes (id, degree, geom)
    SELECT cluster + 1, 0, ST_Transform(ST_Centroid(ST_Collect(pt)), 4326)
    FROM _pts GROUP BY cluster;

    INSERT INTO atlas.routing_edges (id, kind, segment_id, source, target, start_m, end_m, length_m, geom)
    SELECT row_number() OVER (ORDER BY e.kind DESC, e.segment_id, e.start_m), e.*
    FROM (
        SELECT 'street' AS kind, p.segment_id, a.cluster + 1 AS source, b.cluster + 1 AS target,
               p.m0::real AS start_m, p.m1::real AS end_m, ST_Length(p.g)::real AS length_m, ST_Transform(p.g, 4326) AS geom
        FROM _pieces p
        JOIN _pts a ON a.ref = p.segment_id || ':' || p.m0 AND a.role = 'source'
        JOIN _pts b ON b.ref = p.segment_id || ':' || p.m0 AND b.role = 'target'
        WHERE a.cluster <> b.cluster
        UNION ALL
        -- Two dead ends facing each other yield one connector, not two.
        (SELECT DISTINCT ON (least(a.cluster, b.cluster), greatest(a.cluster, b.cluster))
               'connector', NULL, a.cluster + 1, b.cluster + 1, 0, ST_Distance(g.from_pt, g.to_pt),
               ST_Distance(g.from_pt, g.to_pt), ST_Transform(ST_MakeLine(g.from_pt, g.to_pt), 4326)
        FROM _gaps g
        JOIN _pts a ON a.ref = 'gap:' || g.n AND a.role = 'source'
        JOIN _pts b ON b.ref = 'gap:' || g.n AND b.role = 'target'
        WHERE a.cluster <> b.cluster
        ORDER BY least(a.cluster, b.cluster), greatest(a.cluster, b.cluster), ST_Distance(g.from_pt, g.to_pt))
    ) e;

    -- Islands (typically the far carriageway of a divided road, whose cross
    -- streets stop at the near one): link each smaller component to the nearest
    -- node of a larger one within bridge_m, until nothing changes.
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgrouting') THEN
        FOR i IN 1..20 LOOP
            DROP TABLE IF EXISTS pg_temp._comp;
            CREATE TEMPORARY TABLE _comp ON COMMIT DROP AS
            SELECT c.node::integer AS node, c.component, count(*) OVER (PARTITION BY c.component) AS size
            FROM pgr_connectedComponents('SELECT id, source, target, length_m::float8 AS cost, length_m::float8 AS reverse_cost FROM atlas.routing_edges') c;
            CREATE INDEX ON _comp (node);

            INSERT INTO atlas.routing_edges (id, kind, segment_id, source, target, start_m, end_m, length_m, geom)
            SELECT (SELECT coalesce(max(id), 0) FROM atlas.routing_edges) + row_number() OVER (ORDER BY b.component),
                   'connector', NULL, b.from_node, b.to_node, 0, b.d, b.d, ST_MakeLine(b.from_geom, b.to_geom)
            FROM (
                SELECT DISTINCT ON (a.component) a.component, a.node AS from_node, n.geom AS from_geom,
                       near.id AS to_node, near.geom AS to_geom,
                       ST_Distance(ST_Transform(n.geom, 7856), ST_Transform(near.geom, 7856)) AS d
                FROM _comp a
                JOIN atlas.routing_nodes n ON n.id = a.node
                CROSS JOIN LATERAL (
                    SELECT m.id, m.geom FROM atlas.routing_nodes m JOIN _comp mc ON mc.node = m.id
                    WHERE mc.size > a.size OR (mc.size = a.size AND mc.component > a.component)
                    ORDER BY m.geom <-> n.geom LIMIT 1) near
                WHERE a.size < (SELECT max(size) FROM _comp)
                ORDER BY a.component, d
            ) b
            WHERE b.d <= bridge_m;
            EXIT WHEN NOT FOUND;
        END LOOP;
    END IF;

    UPDATE atlas.routing_nodes n SET degree = d.degree
    FROM (SELECT node, count(*) AS degree FROM (
            SELECT source AS node FROM atlas.routing_edges
            UNION ALL SELECT target FROM atlas.routing_edges) x GROUP BY node) d
    WHERE d.node = n.id;
    DELETE FROM atlas.routing_nodes WHERE degree = 0;

    RETURN QUERY SELECT (SELECT count(*)::integer FROM atlas.routing_nodes),
                        (SELECT count(*)::integer FROM atlas.routing_edges);
END
$$;

-- Wheelchair cost per metre, relative to an ideal footpath.
CREATE FUNCTION atlas.grade_cost_multiplier(grade text)
RETURNS real LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE grade
        WHEN 'good' THEN 1.0
        WHEN 'fair' THEN 1.3
        WHEN 'unknown' THEN 1.6
        WHEN 'poor' THEN 2.5
        WHEN 'no_footpath' THEN 8.0
        ELSE 8.0
    END::real
$$;

-- pgRouting-ready: pgr_dijkstra('SELECT id, source, target, cost, reverse_cost FROM atlas.routing_network', a, b, directed => false)
CREATE MATERIALIZED VIEW atlas.routing_network AS
WITH best AS (
    SELECT segment_id,
        max(grade) FILTER (WHERE side = 'left') AS left_grade,
        max(grade) FILTER (WHERE side = 'right') AS right_grade,
        (array_agg(grade ORDER BY score DESC NULLS LAST))[1] AS best_grade
    FROM atlas.segment_sides GROUP BY segment_id
)
SELECT e.id, e.source, e.target, e.kind, e.segment_id, s.street_name, e.length_m,
       coalesce(b.left_grade, 'unknown') AS left_grade,
       coalesce(b.right_grade, 'unknown') AS right_grade,
       g.grade,
       (e.length_m * atlas.grade_cost_multiplier(g.grade))::float8 AS cost,
       (e.length_m * atlas.grade_cost_multiplier(g.grade))::float8 AS reverse_cost,
       e.geom
FROM atlas.routing_edges e
LEFT JOIN atlas.segments s ON s.id = e.segment_id
LEFT JOIN best b ON b.segment_id = e.segment_id
CROSS JOIN LATERAL (SELECT CASE
    -- Pedestrian streets and shared zones are walkable without a separate footpath.
    WHEN s.highway IN ('pedestrian', 'living_street') AND coalesce(b.best_grade, 'unknown') IN ('no_footpath', 'unknown') THEN 'fair'
    ELSE coalesce(b.best_grade, 'unknown') END AS grade) g;
CREATE UNIQUE INDEX routing_network_pk ON atlas.routing_network (id);
CREATE INDEX routing_network_source ON atlas.routing_network (source);
CREATE INDEX routing_network_target ON atlas.routing_network (target);
CREATE INDEX routing_network_geom ON atlas.routing_network USING gist (geom);
COMMENT ON MATERIALIZED VIEW atlas.routing_network IS 'Undirected wheelchair routing graph. cost = metres x grade multiplier of the better footpath side.';

-- ---------------------------------------------------------------------------
-- Petal Path feedback loop
-- ---------------------------------------------------------------------------

CREATE VIEW atlas.verification_targets AS
WITH latest AS (
    SELECT DISTINCT ON (survey_point_id) survey_point_id, observed_on, footpath, needs_verification, source
    FROM atlas.observations
    WHERE survey_point_id IS NOT NULL
    ORDER BY survey_point_id, observed_on DESC NULLS LAST, id DESC
), classified AS (
    SELECT p.id AS survey_point_id, p.segment_id, p.side, p.facing, p.geom,
           l.observed_on AS last_observed_on, l.source AS last_source,
           CASE
               WHEN l.survey_point_id IS NULL THEN 'no_data'
               WHEN l.needs_verification OR l.footpath IS NULL THEN 'uncertain'
               WHEN l.observed_on IS NULL OR l.observed_on < current_date - interval '3 years' THEN 'stale'
           END AS reason
    FROM atlas.survey_points p
    LEFT JOIN latest l ON l.survey_point_id = p.id
)
SELECT *, CASE reason WHEN 'no_data' THEN 3 WHEN 'uncertain' THEN 2 ELSE 1 END AS priority
FROM classified
WHERE reason IS NOT NULL;
COMMENT ON VIEW atlas.verification_targets IS 'Survey points worth a player visit: no_data (3), uncertain (2) or imagery older than 3 years (1). A newer confident observation removes the target.';

CREATE FUNCTION atlas.refresh_derived() RETURNS void LANGUAGE sql AS $$
    REFRESH MATERIALIZED VIEW atlas.segment_sides;
    REFRESH MATERIALIZED VIEW atlas.routing_network;
$$;
COMMENT ON FUNCTION atlas.refresh_derived() IS 'Recompute segment_sides and routing_network after observations change.';

-- ---------------------------------------------------------------------------
-- Access
-- ---------------------------------------------------------------------------

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_read') THEN
    CREATE ROLE atlas_read NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA atlas TO atlas_read;
GRANT SELECT ON ALL TABLES IN SCHEMA atlas TO atlas_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA atlas GRANT SELECT ON TABLES TO atlas_read;
