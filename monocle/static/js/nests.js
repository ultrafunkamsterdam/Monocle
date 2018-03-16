var PokemonIcon = L.Icon.extend({
    options: {
        popupAnchor: [0, -15]
    },
    createIcon: function() {
        var div = document.createElement('div');
        div.innerHTML =
            '<div class="pokemarker">' +
              '<div class="pokeimg">' +
                   '<img class="leaflet-marker-icon" src="' + this.options.iconUrl + '" />' +
              '</div>';
        return div;
    }
});

var markers = {};
var overlays = {
    Nests:     L.layerGroup([]),
    Spawns:    L.layerGroup([]),
    Parks:     L.layerGroup([]),
    L12Cells:  L.layerGroup([]),
    ScanArea:  L.layerGroup([])
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

monitor(overlays.Nests, false)
monitor(overlays.Spawns, true)

function getPopupContent (item) {
    var content = '<b>' + item.name + '</b> - <a href="https://pokemongo.gamepress.gg/pokemon/' + item.pokemon_id + '">#' + item.pokemon_id + '</a>';
    content += '<br>Alternatively:' + item.alternatives + '<br>';
    content += '<br>=&gt; <a href="https://www.google.com/maps/dir/?api=1&destination='+ item.lat + ','+ item.lon +'" target="_blank" title="See in Google Maps">Get directions</a>';
    return content;
}

function PokemonMarker (raw) {
    var icon = new PokemonIcon({iconUrl: '/static/monocle-icons/icons/' + String(raw.pokemon_id) + '.png'});
    var marker = L.marker([raw.lat, raw.lon], {icon: icon, opacity: 1});

    marker.overlay = 'Nests';

    marker.raw = raw;
    markers[raw.id] = marker;
    marker.on('popupopen',function popupopen (event) {
        event.popup.options.autoPan = true; // Pan into view once
        event.popup.setContent(getPopupContent(event.target.raw));
        event.target.popupInterval = setInterval(function () {
            event.popup.setContent(getPopupContent(event.target.raw));
            event.popup.options.autoPan = false; // Don't fight user panning
        }, 1000);
    });
    marker.on('popupclose', function (event) {
        clearInterval(event.target.popupInterval);
    });
    marker.bindPopup();
    return marker;
}

function addPokemonToMap (data, map) {
    data.forEach(function (item) {
        var marker = PokemonMarker(item);
        if (marker.overlay !== "Hidden"){
            marker.addTo(overlays[marker.overlay])
        }
    });
}

function addSpawnsToMap (data, map) {
    data.forEach(function (item) {
        var circle = L.circle([item.lat, item.lon], 5, {weight: 2});
	var time = '??'
        if (item.despawn_time != null) {
            time = '' + Math.floor(item.despawn_time/60) + 'min ' +
                   (item.despawn_time%60) + 'sec';
        }
        else {
            circle.setStyle({color: '#f03'})
        }
        circle.bindPopup('<b>Spawn ' + item.spawn_id + '</b>' +
                         '<br/>despawn: ' + time +
                         '<br/>duration: '+ (item.duration == null ? '30mn' : item.duration + 'mn') +
                         '<br>=&gt; <a href="https://www.google.com/maps/dir/?api=1&destination='+ item.lat + ','+ item.lon +'" target="_blank" title="See in Google Maps">Get directions</a>');
        circle.addTo(overlays.Spawns);
    });
}

function addParksToMap (data, map) {
    data.forEach(function (item) {
        L.polygon(item.coords, {'color': 'limegreen'}).addTo(overlays.Parks);
    });
}

function addL12CellsToMap (data, map) {
    data.forEach(function (item) {
        L.polygon(item.coords, {'color': 'grey'}).addTo(overlays.L12Cells);
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

function getPokemon () {
    new Promise(function (resolve, reject) {
        $.get('/nest_spawns?pokes', function (response) {
            resolve(response);
        });
    }).then(function (data) {
        addPokemonToMap(data, map);
    });
}

function getSpawnPoints() {
    new Promise(function (resolve, reject) {
        $.get('/nest_spawns', function (response) {
            resolve(response);
        });
    }).then(function (data) {
        addSpawnsToMap(data, map);
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
        addL12CellsToMap(data, map);
    });
}

function getL12Cells() {
    if (overlays.L12Cells.hidden) {
        return;
    }
    new Promise(function (resolve, reject) {
        $.get('/L12cells', function (response) {
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
overlays.Nests.addTo(map);

var control = L.control.layers(null, overlays).addTo(map);
L.tileLayer(_MapProviderUrl, {
    opacity: 0.75,
    attribution: _MapProviderAttribution
}).addTo(map);
map.whenReady(function () {
    $('.my-location').on('click', function () {
        map.locate({ enableHighAccurracy: true, setView: true });
    });
    overlays.Parks.once('add', function(e) {
        getParks();
    })
    overlays.L12Cells.once('add', function(e) {
        getL12Cells();
    })
    overlays.Spawns.once('add', function(e) {
        getSpawnPoints();
    })
    getPokemon();
    getScanAreaCoords();
});
