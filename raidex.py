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

from shapely.geometry import Polygon, Point


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
        for p in parks:
            if Polygon(p['coords']).contains(Point(g['lat'], g['lon'])):
                gyms.append(g)
    return jsonify(gyms)

@app.route('/parks')
def parks():
    return jsonify(get_all_parks())

@app.route('/cells')
def cells():
    return jsonify(get_s2_cells())

@app.route('/scan_coords')
def scan_coords():
    return jsonify(get_scan_coords())

def main():
    args = get_args()
    app.run(debug=args.debug, threaded=True, host=args.host, port=args.port)

if __name__ == '__main__':
    main()
