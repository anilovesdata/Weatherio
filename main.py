from fastapi import FastAPI, Request, HTTPException, Body
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
import time

from backend.api_keys import AGRO_API_KEY

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Constants for crop-health (previously missing)
MAX_CLOUD_PERCENT = 20
NDVI_HEALTHY = 0.55      # Adjusted for maize in Nigeria
NDVI_MODERATE = 0.38

@app.get("/", response_class=HTMLResponse)
def show_form(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})

@app.get("/weather")
async def get_weather(latitude: float, longitude: float):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,wind_speed_10m",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
        "forecast_days": 14
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(OPEN_METEO_URL, params=params)

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Failed to fetch weather: {response.text[:150]}")

    data = response.json()

    daily_data = data.get("daily", {})
    if not daily_data or "time" not in daily_data:
        raise HTTPException(status_code=500, detail="No valid daily data returned")

    daily_summary = []
    for i in range(len(daily_data["time"])):
        avg_temp = (daily_data["temperature_2m_max"][i] + daily_data["temperature_2m_min"][i]) / 2
        daily_summary.append({
            "date": daily_data["time"][i],
            "avg_temp": round(avg_temp, 2),
            "max_temp": round(daily_data["temperature_2m_max"][i], 2),
            "min_temp": round(daily_data["temperature_2m_min"][i], 2),
            "total_rainfall_mm": round(daily_data["precipitation_sum"][i], 2),
            "moisture_indicator": "High" if daily_data["precipitation_sum"][i] > 5 else "Low"
        })

    if len(daily_summary) < 7:
        raise HTTPException(status_code=500, detail="Not enough forecast days")

    upcoming_7_days = daily_summary[:7]
    avg_7day_temp = sum(d["avg_temp"] for d in upcoming_7_days) / 7
    total_7day_rainfall = sum(d["total_rainfall_mm"] for d in upcoming_7_days)
    rainy_days = sum(1 for d in upcoming_7_days if d["total_rainfall_mm"] >= 5)

    maize_conditions = {
        "temperature_ok": 25 <= avg_7day_temp <= 32,
        "rain_incoming": total_7day_rainfall >= 30,
        "consistent_moisture": rainy_days >= 3,
        "no_extreme_heat": all(d["max_temp"] <= 35 for d in upcoming_7_days)
    }

    if all(maize_conditions.values()):
        recommendation = "PLANT MAIZE NOW 🌽 Optimal window ahead!"
        details = "Rains starting soon + perfect temps. Prepare land!"
    elif total_7day_rainfall >= 15:
        recommendation = "PREPARE TO PLANT SOON ⏳"
        details = "Some rain coming – good if you have irrigation backup."
    else:
        recommendation = "WAIT FOR RAINY SEASON ⏳ (March–June best)"
        details = f"Dry forecast ({total_7day_rainfall}mm next week). Risk of poor germination without irrigation."

    return {
        "location": {
            "latitude": data["latitude"],
            "longitude": data["longitude"],
            "elevation": data.get("elevation", "N/A")
        },
        "crop": "maize (corn)",
        "daily_summary_next_14_days": daily_summary,
        "next_7_days_analysis": {
            "avg_temp": round(avg_7day_temp, 2),
            "total_rainfall_mm": round(total_7day_rainfall, 2),
            "rainy_days_count": rainy_days,
            "conditions_met": maize_conditions
        },
        "recommendation": recommendation,
        "advice": details
    }

@app.post("/create-polygon")
async def create_polygon(payload: dict = Body(...)):
    geo_json = payload.get("geo_json")
    if not geo_json or geo_json.get("type") != "Feature" or not geo_json.get("geometry"):
        raise HTTPException(400, detail="Invalid GeoJSON: must be Feature with geometry")

    # Reverse coordinates: Leaflet [lat, lon] → Agro [lon, lat]
    def reverse_coords(coords):
        if isinstance(coords[0], list):  # nested
            return [reverse_coords(c) for c in coords]
        return [coords[1], coords[0]]

    geometry = geo_json["geometry"]
    if geometry["type"] == "Polygon":
        geometry["coordinates"] = [reverse_coords(ring) for ring in geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        geometry["coordinates"] = [[reverse_coords(ring) for ring in poly] for poly in geometry["coordinates"]]

    agro_payload = {
        "name": f"Farm from App - {time.strftime('%Y-%m-%d %H:%M')}",
        "geo_json": geo_json
    }

    url = "https://api.agromonitoring.com/agro/1.0/polygons"  # Fixed to HTTPS

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            url,
            json=agro_payload,
            params={"appid": AGRO_API_KEY}
        )

    if resp.status_code not in (200, 201):
        error_detail = resp.text[:200] or "Unknown error"
        raise HTTPException(status_code=resp.status_code, detail=f"Agro API failed: {error_detail}")

    created = resp.json()
    poly_id = created.get("_id") or created.get("id")

    if not poly_id:
        raise HTTPException(500, detail="No _id returned from Agro API")

    return {"poly_id": poly_id, "message": "Polygon created successfully"}

@app.get("/crop-health")
async def get_crop_health(poly_id: str = "your_polygon_id_here", days_lookback: int = 30):
    end_time = int(time.time())
    start_time = end_time - (days_lookback * 86400)

    search_url = "https://api.agromonitoring.com/agro/1.0/image/search"
    search_params = {
        "appid": AGRO_API_KEY,
        "polyid": poly_id,
        "start": start_time,
        "end": end_time,
        "clouds": MAX_CLOUD_PERCENT
    }

    # Use longer timeout for the whole client + individual requests
    async with httpx.AsyncClient(timeout=60.0) as client:  # Overall client timeout: 60 seconds
        try:
            search_resp = await client.get(search_url, params=search_params, timeout=40.0)  # Search: 40s

            if search_resp.status_code != 200:
                raise HTTPException(502, detail=f"Search failed: {search_resp.text[:150]}")

            images = search_resp.json()

            if not images:
                return {
                    "status": "no_image",
                    "message": "No clear satellite images found in the last 30 days (common in harmattan/dry season in Lagos).",
                    "tip": "Try again in 3–7 days or draw polygon over greener area."
                }

            # Sort to get the newest image
            images_sorted = sorted(images, key=lambda x: x.get("dt", 0), reverse=True)
            latest_image = images_sorted[0]

            ndvi_stats_url = latest_image.get("stats", {}).get("ndvi")
            if not ndvi_stats_url:
                raise HTTPException(500, detail="No NDVI stats URL found in image data")

            # Safe appid append
            if "?" not in ndvi_stats_url:
                ndvi_stats_url += f"?appid={AGRO_API_KEY}"

            # Give NDVI stats fetch extra time (this is the slow part)
            ndvi_resp = await client.get(ndvi_stats_url, timeout=90.0)  # Increased to 90 seconds!

            if ndvi_resp.status_code != 200:
                raise HTTPException(502, detail=f"NDVI stats failed: {ndvi_resp.text[:150]}")

            ndvi_stats = ndvi_resp.json()
            mean_ndvi = ndvi_stats.get("mean")

            if mean_ndvi is None:
                raise HTTPException(500, detail="No NDVI mean value available")

            if mean_ndvi >= NDVI_HEALTHY:
                health = "Healthy 🌿"
                advice = "Crops look strong. Keep it up!"
            elif mean_ndvi >= NDVI_MODERATE:
                health = "Moderate Stress ⚠️"
                advice = "Some stress detected — check water/nutrients soon."
            else:
                health = "Poor Health ❌"
                advice = "Crop struggling — act fast (water, pests, nutrients?)."

            return {
                "polygon_id": poly_id,
                "ndvi_mean": round(mean_ndvi, 3),
                "health_status": health,
                "advice": advice,
                "satellite_date": latest_image.get("dt"),
                "truecolor_image": latest_image.get("image", {}).get("truecolor", "N/A")
            }

        except httpx.ReadTimeout:
            return {
                "status": "timeout",
                "message": "Agro API is taking too long to respond (common during harmattan season or server load).",
                "tip": "Try again in 10–30 minutes or tomorrow. No data lost — your polygon ID is still valid."
            }
        except Exception as e:
            raise HTTPException(500, detail=f"Unexpected error: {str(e)[:150]}")