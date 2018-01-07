var FortIcon = L.Icon.extend({
    options: {
        iconSize: [20, 20],
        popupAnchor: [0, -10],
        className: 'fort-icon'
    }
});

var markers = {};
var overlays = {
    Gyms: L.layerGroup([]),
    Parks: L.layerGroup([]),
    Cells: L.layerGroup([]),
    ScanArea: L.layerGroup([])
};

function unsetHidden (event) {
    event.target.hidden = false;
}

function setHidden (event) {
    event.target.hidden = true;
}

function monitor (group, initial) {
    group.hidden = initial;
    group.on('add', unsetHidden);
    group.on('remove', setHidden);
}

monitor(overlays.Gyms, true)

function FortMarker (raw) {
    var icon = new FortIcon({iconUrl: '/static/monocle-icons/forts/' + raw.team + '.png'});
    var marker = L.marker([raw.lat, raw.lon], {icon: icon, opacity: 1});
    marker.raw = raw;
    markers[raw.id] = marker;
    marker.on('popupopen',function popupopen (event) {
        var content = ''
        content += '<br>=&gt; <a href=https://www.google.com/maps/?daddr='+ raw.lat + ','+ raw.lon +' target="_blank" title="See in Google Maps">Get directions</a>';
        event.popup.setContent(content);
    });
    marker.bindPopup();
    return marker;
}

function addGymsToMap (data, map) {
    data.forEach(function (item) {
        // No change since last time? Then don't do anything
        var existing = markers[item.id];
        if (typeof existing !== 'undefined') {
            if (existing.raw.sighting_id === item.sighting_id) {
                return;
            }
            existing.removeFrom(overlays.Gyms);
            markers[item.id] = undefined;
        }
        marker = FortMarker(item);
        marker.addTo(overlays.Gyms);
    });
}

function addParksToMap (data, map) {
    data.forEach(function (item) {
        L.polygon(item.coords, {'color': 'limegreen'}).addTo(overlays.Parks);
    });
}

function addCellsToMap (data, map) {
    data.forEach(function (item) {
        L.polygon(item.coords, {'color': 'grey'}).addTo(overlays.Cells);
    });
}

function addScanAreaToMap (data, map) {
    data.forEach(function (item) {
        if (item.type === 'scanarea'){
            L.polyline(item.coords).addTo(overlays.ScanArea);
        } else if (item.type === 'scanblacklist'){
            L.polyline(item.coords, {'color':'red'}).addTo(overlays.ScanArea);
        }
    });
}

function getGyms() {
    if (overlays.Gyms.hidden) {
        return;
    }
    new Promise(function (resolve, reject) {
        $.get('/gym_data', function (response) {
            resolve(response);
        });
    }).then(function (data) {
        addGymsToMap(data, map);
    });
}

function getParks() {
    if (overlays.Parks.hidden) {
        return;
    }
    new Promise(function (resolve, reject) {
        $.get('/parks', function (response) {
            resolve(response);
        });
    }).then(function (data) {
        addParksToMap(data, map);
    });
}

function getCells() {
    if (overlays.Cells.hidden) {
        return;
    }
    new Promise(function (resolve, reject) {
        $.get('/cells', function (response) {
            resolve(response);
        });
    }).then(function (data) {
        addCellsToMap(data, map);
    });
}

function getScanAreaCoords() {
    new Promise(function (resolve, reject) {
        $.get('/scan_coords', function (response) {
            resolve(response);
        });
    }).then(function (data) {
        addScanAreaToMap(data, map);
    });
}

var map = L.map('main-map', {preferCanvas: true}).setView(_MapCoords, 13);

overlays.ScanArea.addTo(map);

var control = L.control.layers(null, overlays).addTo(map);
L.tileLayer(_MapProviderUrl, {
    opacity: 0.75,
    attribution: _MapProviderAttribution
}).addTo(map);
map.whenReady(function () {
    $('.my-location').on('click', function () {
        map.locate({ enableHighAccurracy: true, setView: true });
    });
    overlays.Gyms.once('add', function(e) {
        getGyms();
    })
    overlays.Parks.once('add', function(e) {
        getParks();
    })
    overlays.Cells.once('add', function(e) {
        getCells();
    })
    getScanAreaCoords();
});
