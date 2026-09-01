from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simulated spatial database storage (GeoJSON FeatureCollection)
PARCELS_DB = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"id": 41, "uplin": "09121050550041", "name": "Commercial Block"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[77.1035, 28.7040], [77.1044, 28.7040], [77.1044, 28.7048], [77.1035, 28.7048], [77.1035, 28.7040]]]
            }
        },
        {
            "type": "Feature",
            "properties": {"id": 42, "uplin": "09121050550042", "name": "Residential Tower"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[77.1045, 28.7050], [77.1055, 28.7050], [77.1055, 28.7058], [77.1045, 28.7058], [77.1045, 28.7050]]]
            }
        }
    ]
}

@app.get("/api/v1/parcels")
async def get_parcels():
    return {"success": True, "data": PARCELS_DB}

@app.post("/api/v1/generate-3d-parcel")
async def generate_3d_parcel(payload: dict):
    # Your existing 3D extrusion handler
    return {"success": True, "data": {"geometry": payload["geometry"], "properties": payload["properties"]}}