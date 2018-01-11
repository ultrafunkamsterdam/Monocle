#!/usr/bin/env python3

from datetime import datetime
from pkg_resources import resource_filename

try:
    from ujson import dumps
    from flask import json as flask_json
    flask_json.dumps = lambda obj, **kwargs: dumps(obj, double_precision=6)
except ImportError:
    from json import dumps

from flask import Flask, jsonify, Markup, render_template, request

from monocle import db, sanitized as conf
from monocle.web_utils import *
from monocle.bounds import area, center

from shapely.geometry import Polygon, Point, LineString
import s2sphere

app = Flask(__name__, template_folder=resource_filename('monocle', 'templates'), static_folder=resource_filename('monocle', 'static'))

def render_map():
    template = app.jinja_env.get_template('raidex.html')
    return template.render(
        area_name=conf.AREA_NAME,
        map_center=center,
        map_provider_url=conf.MAP_PROVIDER_URL,
        map_provider_attribution=conf.MAP_PROVIDER_ATTRIBUTION
    )
    
@app.route('/')
def fullmap(map_html=render_map()):
    return map_html

@app.route('/gym_data')
def gym_data():
    gyms = []
    parks = get_all_parks()
    for g in get_gym_markers():
        gym_point = Point(g['lat'], g['lon'])
        cell = Polygon(get_s2_cell_as_polygon(g['lat'], g['lon'], 20)) # s2 lvl 20
        for p in parks:
            coords = p['coords']
            # osm polygon can be a line
            if len(coords) == 2:
                shape = LineString(coords)
                if shape.within(gym_point) or cell.intersects(shape):
                    gyms.append(g)
                    break
            if len(coords) > 2:
                shape = Polygon(coords)
                if shape.contains(gym_point) or cell.intersects(shape):
                    gyms.append(g)
                    break
    return jsonify(gyms)

@app.route('/parks_cells')
def parks_cells():
    markers = []
    parks = get_all_parks()
    for g in get_gym_markers():
        gym_point = Point(g['lat'], g['lon'])
        cell = Polygon(get_s2_cell_as_polygon(g['lat'], g['lon'], 20)) # s2 lvl 20
        for p in parks:
            coords = p['coords']
            if len(coords) > 2:
                shape = Polygon(coords)
                if shape.contains(gym_point) or cell.intersects(shape):
                    bounds = shape.bounds
                    markers += get_s2_cells(bounds[0], bounds[1], bounds[2], bounds[3], 20)
                    break
    return jsonify(markers)

@app.route('/parks')
def parks():
    return jsonify(get_all_parks())

@app.route('/cells')
def cells():
    return jsonify(get_s2_cells(level=12))

@app.route('/scan_coords')
def scan_coords():
    return jsonify(get_scan_coords())

def main():
    args = get_args()
    app.run(debug=args.debug, threaded=True, host=args.host, port=args.port)

if __name__ == '__main__':
    main()
