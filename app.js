let map;
let drawnItems = new L.FeatureGroup();
let lastLayer = null;

async function getLocation() {
    console.log("Button clicked");

    if (!navigator.geolocation) {
        alert("GPS not supported");
        return;
    }

    navigator.geolocation.getCurrentPosition(
        async (pos) => {
            const lat = pos.coords.latitude;
            const lon = pos.coords.longitude;

            // 1️⃣ Fetch weather
            const res = await fetch(`/weather?latitude=${lat}&longitude=${lon}`);
            const data = await res.json();
            document.getElementById("result").textContent = JSON.stringify(data, null, 2);

            // 2️⃣ Initialize map centered at user location
            if (!map) {
                map = L.map('map').setView([lat, lon], 13);
                L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                    attribution: '© OpenStreetMap contributors'
                }).addTo(map);

                map.addLayer(drawnItems);

                const drawControl = new L.Control.Draw({
                    draw: { marker: false, polyline: false, circle: false, rectangle: false, circlemarker: false },
                    edit: { featureGroup: drawnItems }
                });
                map.addControl(drawControl);

                map.on(L.Draw.Event.CREATED, function (e) {
                    if (lastLayer) drawnItems.removeLayer(lastLayer);
                    lastLayer = e.layer;
                    drawnItems.addLayer(lastLayer);
                });
            } else {
                map.setView([lat, lon], 13); // recenter if map exists
            }
        },
        (err) => {
            console.error("Error getting location:", err);
            alert("Failed to get location: " + err.message);
        }
    );
}

// Polygon save button logic
document.getElementById('savePolygon').onclick = async () => {
    if (!lastLayer) {
        alert("Draw a polygon first!");
        return;
    }

    const geojson = lastLayer.toGeoJSON();
    const response = await fetch('/create-polygon', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ geo_json: geojson })
    });

    const data = await response.json();
    document.getElementById('result').textContent += `\nPolygon saved! ID: ${data.poly_id}`;
};
