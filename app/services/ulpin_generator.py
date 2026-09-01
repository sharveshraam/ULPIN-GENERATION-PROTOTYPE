from shapely.geometry import shape, Polygon
import pyproj


def calculate_centroid_latlon(geom_json: dict) -> tuple:
    """Computes latitude and longitude centroid from a GeoJSON polygon geometry."""
    geom = shape(geom_json)
    centroid = geom.centroid
    # If coordinate system is projected, convert back to WGS84 (Lat/Lon)
    return centroid.y, centroid.x


def generate_ulpin_code(
    state_code: str,
    district_code: str,
    sub_district_code: str,
    village_code: str,
    plot_number: int,
) -> str:
    """
    Generates the 14-digit standard Indian ULPIN structure:
    [State(2)][District(2)][Sub-District(3)][Village(3)][Plot/Survey(4)]
    """
    return f"{state_code.zfill(2)}{district_code.zfill(2)}{sub_district_code.zfill(3)}{village_code.zfill(3)}{str(plot_number).zfill(4)}"


def process_parcel_data(feature_data: dict) -> dict:
    coords = calculate_centroid_latlon(feature_data["geometry"])
    props = feature_data.get("properties", {})

    ulpin = generate_ulpin_code(
        state_code=props.get("state_code", "09"),
        district_code=props.get("district_code", "12"),
        sub_district_code=props.get("sub_district_code", "105"),
        village_code=props.get("village_code", "055"),
        plot_number=props.get("plot_number", 1),
    )

    return {
        "ulpin": ulpin,
        "centroid_latitude": coords[0],
        "centroid_longitude": coords[1],
        "parcel_area_sq_m": shape(feature_data["geometry"]).area,
        "properties": props,
    }


def generate_3d_polygon(feature_data: dict, height_meters: float = 10.0) -> dict:
    """
    Extrudes a 2D GeoJSON polygon into a 3D structure by adding elevation/height properties.
    """
    geom = shape(feature_data["geometry"])
    coords = list(geom.exterior.coords)

    # Create 3D coordinates (Longitude, Latitude, Elevation)
    coords_3d = [[lon, lat, height_meters] for lon, lat in coords]

    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coords_3d]},
        "properties": {
            **feature_data.get("properties", {}),
            "height_m": height_meters,
            "base_height_m": 0.0,
        },
    }
