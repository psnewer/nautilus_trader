# -*- coding: utf-8 -*-
"""
Tests for Web Panel Application
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import json

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

# Import FastAPI test client
from fastapi.testclient import TestClient


class TestWebPanelApp:
    """Tests for Web Panel FastAPI Application"""

    @pytest.fixture
    def client(self):
        """Create test client"""
        from web_panel.app import app
        return TestClient(app)

    @pytest.fixture
    def output_dir(self, tmp_path):
        """Create temporary output directory"""
        output = tmp_path / "output"
        output.mkdir()
        return output

    def test_health_check(self, client):
        """Test health check endpoint"""
        response = client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data

    def test_index_page(self, client):
        """Test index page loads"""
        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_discovery_page(self, client):
        """Test discovery page loads"""
        response = client.get("/discovery")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_matching_page(self, client):
        """Test matching page loads"""
        response = client.get("/matching")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_config_page(self, client):
        """Test config page loads"""
        response = client.get("/config")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestDiscoveryAPI:
    """Tests for Discovery API endpoints"""

    @pytest.fixture
    def client(self):
        """Create test client"""
        from web_panel.app import app
        return TestClient(app)

    def test_get_polymarket_events_empty(self, client, tmp_path):
        """Test getting Polymarket events when file doesn't exist"""
        with patch("web_panel.app.output_dir", tmp_path):
            response = client.get("/api/discovery/polymarket-events")

            assert response.status_code == 200
            data = response.json()
            assert data["events"] == []
            assert data["count"] == 0

    def test_get_orbitexch_events_empty(self, client, tmp_path):
        """Test getting OrbitExch events when file doesn't exist"""
        with patch("web_panel.app.output_dir", tmp_path):
            response = client.get("/api/discovery/orbitexch-events")

            assert response.status_code == 200
            data = response.json()
            assert data["events"] == []
            assert data["count"] == 0

    def test_get_polymarket_events_with_data(self, client, tmp_path):
        """Test getting Polymarket events with existing data"""
        # Create test data file
        events = [
            {"event_id": "1", "sport": "Soccer", "event": "Test Match"}
        ]
        events_file = tmp_path / "polymarket_events.json"
        events_file.write_text(json.dumps(events))

        with patch("web_panel.app.output_dir", tmp_path):
            response = client.get("/api/discovery/polymarket-events")

            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1
            assert len(data["events"]) == 1

    def test_get_orbitexch_events_with_data(self, client, tmp_path):
        """Test getting OrbitExch events with existing data"""
        # Create test data file
        events = [
            {"event_id": "1", "sport": "Basketball", "event": "NBA Game"}
        ]
        events_file = tmp_path / "orbitexch_events.json"
        events_file.write_text(json.dumps(events))

        with patch("web_panel.app.output_dir", tmp_path):
            response = client.get("/api/discovery/orbitexch-events")

            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1

    def test_start_polymarket_crawler_script_not_found(self, client):
        """Test starting Polymarket crawler when script doesn't exist"""
        with patch("web_panel.app.POLYMARKET_SCRIPT", "/nonexistent/script.py"):
            response = client.post("/api/discovery/start-polymarket")

            assert response.status_code == 404
            data = response.json()
            assert data["success"] is False

    def test_start_orbitexch_crawler_script_not_found(self, client):
        """Test starting OrbitExch crawler when script doesn't exist"""
        with patch("web_panel.app.ORBITEXCH_SCRIPT", "/nonexistent/script.py"):
            response = client.post("/api/discovery/start-orbitexch")

            assert response.status_code == 404
            data = response.json()
            assert data["success"] is False


class TestMatchingAPI:
    """Tests for Matching API endpoints"""

    @pytest.fixture
    def client(self):
        """Create test client"""
        from web_panel.app import app
        return TestClient(app)

    def test_get_matching_results_empty(self, client, tmp_path):
        """Test getting matching results when no data exists"""
        with patch("web_panel.app.output_dir", tmp_path):
            response = client.get("/api/matching/results")

            assert response.status_code == 200
            data = response.json()
            assert data["matches"] == []
            assert data["match_count"] == 0

    def test_get_matching_results_with_data(self, client, tmp_path):
        """Test getting matching results with existing data"""
        # Create test data files
        poly_events = [{"event_id": "p1"}]
        orbit_events = [{"event_id": "o1"}]
        matches = [
            {
                "polymarket_event": {"event_id": "p1"},
                "orbitexch_event": {"event_id": "o1"},
                "confidence": 0.85
            }
        ]

        (tmp_path / "polymarket_events.json").write_text(json.dumps(poly_events))
        (tmp_path / "orbitexch_events.json").write_text(json.dumps(orbit_events))
        (tmp_path / "market_matches.json").write_text(json.dumps(matches))

        with patch("web_panel.app.output_dir", tmp_path):
            response = client.get("/api/matching/results")

            assert response.status_code == 200
            data = response.json()
            assert data["match_count"] == 1
            assert data["polymarket_count"] == 1
            assert data["orbitexch_count"] == 1

    def test_start_matching_missing_input(self, client, tmp_path):
        """Test starting matching when input files are missing"""
        with patch("web_panel.app.output_dir", tmp_path), \
             patch("web_panel.app.MATCHER_SCRIPT", "existing_script.py"), \
             patch("pathlib.Path.exists", return_value=True):

            # But output files don't exist
            response = client.post("/api/matching/start")

            # Should fail because input files don't exist
            assert response.status_code in [400, 404, 500]


class TestConfigAPI:
    """Tests for Config API endpoints"""

    @pytest.fixture
    def client(self):
        """Create test client"""
        from web_panel.app import app
        return TestClient(app)

    def test_get_config_no_file(self, client, tmp_path):
        """Test getting config when file doesn't exist"""
        with patch("web_panel.app.Path") as mock_path:
            mock_path.return_value.exists.return_value = False

            response = client.get("/api/config")

            assert response.status_code == 200
            data = response.json()
            assert "config" in data

    def test_update_config(self, client, tmp_path):
        """Test updating configuration"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        new_config = {
            "discovery": {
                "interval": 120
            }
        }

        with patch("web_panel.app.Path") as mock_path:
            mock_file = MagicMock()
            mock_path.return_value = mock_file
            mock_file.parent.mkdir = MagicMock()
            mock_file.open = MagicMock()

            response = client.post("/api/config", json=new_config)

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
