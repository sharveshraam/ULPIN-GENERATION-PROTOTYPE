from fastapi import FastAPI, HTTPException
from app.services.ulpin_generator import process_parcel_data

app = FastAPI(title="3D ULPIN Geospatial Backend")


@app.post("/api/v1/generate-ulpin")
def create_ulpin(feature: dict):
    try:
        result = process_parcel_data(feature)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
