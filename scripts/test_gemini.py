"""Test Gemini / Vertex AI connectivity locally and check live response."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings, ensure_gcp_credentials
from app.importers import load_production
from app.agents.intake import parse_incident, FallbackRequired
from app.store import Store


def main() -> None:
    ensure_gcp_credentials()
    settings = get_settings()
    import google.genai as _genai

    print("=" * 60)
    print("🎬 Backlot Dispatch — Gemini / Vertex AI Test")
    print("=" * 60)
    print(f"• google-genai SDK:    {_genai.__version__} (3.x models need >= 2.20)")
    print(f"• Project ID:          {settings.project_id or '(Not configured)'}")
    print(f"• Use Vertex AI:       {settings.use_vertexai}")
    print(f"• Vertex Location:     {settings.gemini_location}")
    print(f"• Gemini Model:        {settings.gemini_model}")
    print(f"• Thinking Level:      {settings.gemini_thinking_level}")
    print(f"• Gemini Configured:   {settings.gemini_configured}")
    print(f"• Credentials Env:     {os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '(Not set)')}")

    print("-" * 60)
    print("Loading production locations from seed/ ...")
    production, _ = load_production(Path("seed"))
    locations = production.locations
    print(f"Found {len(locations)} locations: {', '.join(locations.keys())}")

    print("-" * 60)
    sample_text = "Generator failure at Stage 4, crew says exterior unit blocked until 14:00."
    print(f"Testing Intake Agent with text:\n\"{sample_text}\"\n")

    store = Store(Path("instance/test_backlot.db"))
    try:
        incident = parse_incident(sample_text, settings, locations, store=store)
        print("✅ SUCCESS! Gemini call returned structured data:")
        print(f"  • Incident Type:   {incident.type}")
        print(f"  • Location ID:     {incident.location_id}")
        print(f"  • Blocked Until:   {incident.blocked_until}")
        print(f"  • Severity:        {incident.severity}")
        print(f"  • Source:          {incident.source}")
        
        # Check GCP log
        logs = store.recent_gcp_calls(limit=1)
        if logs:
            log = logs[0]
            print(f"  • Latency:         {log.get('latency_ms')} ms")
            print(f"  • Status OK:       {bool(log.get('ok'))}")
            print(f"  • Meta:            {log.get('meta_json')}")
    except FallbackRequired as exc:
        print(f"⚠️ Fallback triggered: {exc}")
        print("Check your GCP credentials, project ID, or model name in .env")
    except Exception as exc:
        print(f"❌ Error during test: {exc}")

    print("=" * 60)


if __name__ == "__main__":
    main()
