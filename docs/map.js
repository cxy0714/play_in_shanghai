/* 上海公共建筑地图 · 交互逻辑 */
(function () {
  "use strict";

  var dataEl = document.getElementById("places-data");
  if (!dataEl) return;

  var DATA;
  try {
    DATA = JSON.parse(dataEl.textContent || "{}");
  } catch (error) {
    return;
  }

  var CATS = {};
  (DATA.categories || []).forEach(function (cat) {
    CATS[cat.key] = cat;
  });

  var PLACES = DATA.places || [];
  var bySlug = {};
  PLACES.forEach(function (place) {
    var cat = CATS[place.category] || { label: "" };
    place.searchText = [
      place.name,
      place.district,
      place.address,
      place.intro,
      cat.label
    ].join(" ").toLowerCase();
    bySlug[place.slug] = place;
  });

  var state = { cat: "all", q: "", base: "amap", selected: null };
  var els = {
    list: document.getElementById("place-list"),
    empty: document.getElementById("place-empty"),
    count: document.getElementById("result-count"),
    search: document.getElementById("place-search"),
    status: document.getElementById("map-status"),
    tip: document.querySelector(".map-tip")
  };

  var hasLeaflet = typeof window.L !== "undefined";
  var hasCluster = hasLeaflet && typeof window.L.markerClusterGroup === "function";
  var map = null;
  var cluster = null;
  var layers = null;
  var markers = {};
  var prefersReduced = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function project(place) {
    return state.base === "amap" ? place.gcj : place.wgs;
  }

  function cityCenter() {
    return DATA.center[state.base === "amap" ? "gcj" : "wgs"];
  }

  /* ---------- WGS-84 / GCJ-02：切换底图时保持视野位置 ---------- */
  var PI = Math.PI;
  var AXIS = 6378245.0;
  var EE = 0.00669342162296594323;

  function outOfChina(lat, lng) {
    return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55);
  }

  function transformLat(x, y) {
    var ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
    ret += (160.0 * Math.sin(y / 12.0 * PI) + 320.0 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
    return ret;
  }

  function transformLng(x, y) {
    var ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
    ret += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
    return ret;
  }

  function wgsToGcj(lat, lng) {
    if (outOfChina(lat, lng)) return [lat, lng];
    var dLat = transformLat(lng - 105.0, lat - 35.0);
    var dLng = transformLng(lng - 105.0, lat - 35.0);
    var radLat = lat / 180.0 * PI;
    var magic = Math.sin(radLat);
    magic = 1 - EE * magic * magic;
    var sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((AXIS * (1 - EE)) / (magic * sqrtMagic) * PI);
    dLng = (dLng * 180.0) / (AXIS / sqrtMagic * Math.cos(radLat) * PI);
    return [lat + dLat, lng + dLng];
  }

  function gcjToWgs(lat, lng) {
    var wLat = lat;
    var wLng = lng;
    for (var i = 0; i < 3; i += 1) {
      var gcj = wgsToGcj(wLat, wLng);
      wLat -= gcj[0] - lat;
      wLng -= gcj[1] - lng;
    }
    return [wLat, wLng];
  }

  /* ---------- 地图 ---------- */
  function initMap() {
    if (!hasLeaflet) {
      if (els.status) els.status.hidden = false;
      if (els.tip) els.tip.hidden = true;
      return;
    }
    map = L.map("map", {
      zoomControl: false,
      minZoom: 10,
      maxZoom: 18,
      attributionControl: true
    }).setView(cityCenter(), 12);

    layers = {
      amap: L.tileLayer(
        "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
        {
          subdomains: ["1", "2", "3", "4"],
          maxZoom: 18,
          attribution: "&copy; 高德地图"
        }
      ),
      osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors"
      })
    };
    layers[state.base].addTo(map);
    L.control.zoom({ position: "bottomright" }).addTo(map);

    if (hasCluster) {
      cluster = L.markerClusterGroup({
        showCoverageOnHover: false,
        maxClusterRadius: 46,
        spiderfyOnMaxZoom: true,
        disableClusteringAtZoom: 17,
        removeOutsideVisibleBounds: true
      });
      map.addLayer(cluster);
    }

    // 方便调试 / 自动化检查
    window.__placeMap = map;
  }

  function pinIcon(place) {
    var cat = CATS[place.category] || { color: "#666", emoji: "📍" };
    return L.divIcon({
      className: "place-pin",
      html:
        '<span class="pin-inner" style="background:' +
        esc(cat.color) +
        '">' +
        esc(cat.emoji) +
        "</span>",
      iconSize: [30, 30],
      iconAnchor: [15, 15],
      popupAnchor: [0, -18]
    });
  }

  function popupHtml(place) {
    var cat = CATS[place.category] || { label: "", color: "#666" };
    var amapUrl =
      "https://uri.amap.com/marker?position=" +
      encodeURIComponent(place.gcj[1] + "," + place.gcj[0]) +
      "&name=" +
      encodeURIComponent(place.name) +
      "&coordinate=gaode&callnative=0&src=play-in-shanghai";
    var baiduUrl =
      "https://www.baidu.com/s?wd=" +
      encodeURIComponent(place.name + " 上海 开放时间");
    var site = place.site
      ? '<a href="' + esc(place.site) + '" target="_blank" rel="noopener noreferrer">官网</a>'
      : "";
    return (
      '<div class="pop">' +
      '<span class="pop-cat" style="color:' +
      esc(cat.color) +
      '">● ' +
      esc(cat.label) +
      "</span>" +
      "<h3>" +
      esc(place.name) +
      "</h3>" +
      '<p class="pop-addr">' +
      esc([place.district, place.address].filter(Boolean).join(" · ")) +
      "</p>" +
      '<p class="pop-intro">' +
      esc(place.intro || "") +
      "</p>" +
      '<div class="pop-links">' +
      '<a href="' +
      amapUrl +
      '" target="_blank" rel="noopener noreferrer">高德导航</a>' +
      '<a href="' +
      baiduUrl +
      '" target="_blank" rel="noopener noreferrer">搜索详情</a>' +
      site +
      "</div></div>"
    );
  }

  function addMarkers(places) {
    if (!map) return;
    var target = cluster || map;
    places.forEach(function (place) {
      var marker = L.marker(project(place), {
        icon: pinIcon(place),
        title: place.name,
        alt: place.name,
        riseOnHover: true
      });
      marker.bindPopup(popupHtml(place), {
        className: "place-popup",
        maxWidth: 280,
        minWidth: 220,
        autoPanPadding: [44, 44]
      });
      marker.on("click", function () {
        setSelected(place.slug, false);
      });
      target.addLayer(marker);
      markers[place.slug] = marker;
    });
  }

  function clearMarkers() {
    Object.keys(markers).forEach(function (slug) {
      delete markers[slug];
    });
    if (cluster) cluster.clearLayers();
    if (map) {
      var loose = [];
      map.eachLayer(function (layer) {
        if (layer instanceof L.Marker) loose.push(layer);
      });
      loose.forEach(function (layer) {
        map.removeLayer(layer);
      });
    }
  }

  function setSelected(slug, fly) {
    var place = bySlug[slug];
    if (!place) return;
    state.selected = slug;

    var items = els.list ? els.list.querySelectorAll(".place-item") : [];
    Array.prototype.forEach.call(items, function (el) {
      el.classList.toggle("active", el.getAttribute("data-slug") === slug);
    });
    var active = els.list
      ? els.list.querySelector('.place-item[data-slug="' + slug + '"]')
      : null;
    if (active && active.scrollIntoView) {
      active.scrollIntoView({
        block: "nearest",
        behavior: prefersReduced ? "auto" : "smooth"
      });
    }

    if (fly && map && markers[slug]) {
      var marker = markers[slug];
      map.setView(project(place), Math.max(map.getZoom(), 15), {
        animate: !prefersReduced
      });
      if (cluster && cluster.zoomToShowLayer) {
        cluster.zoomToShowLayer(marker, function () {
          marker.openPopup();
        });
      } else {
        marker.openPopup();
      }
    }
  }

  function fitVisible(places, maxZoom) {
    if (!map || !places.length) return;
    var points = places.map(project);
    if (points.length === 1) {
      map.setView(points[0], 15, { animate: !prefersReduced });
      return;
    }
    map.fitBounds(L.latLngBounds(points), {
      padding: [36, 36],
      maxZoom: maxZoom || 14,
      animate: !prefersReduced
    });
  }

  function fitCore() {
    if (map) {
      map.setView(cityCenter(), 12, { animate: !prefersReduced });
    }
  }

  /* ---------- 列表与筛选 ---------- */
  function visiblePlaces() {
    var q = state.q.trim().toLowerCase();
    return PLACES.filter(function (place) {
      var catOk = state.cat === "all" || place.category === state.cat;
      var searchOk = !q || place.searchText.indexOf(q) !== -1;
      return catOk && searchOk;
    });
  }

  function renderList(places) {
    if (!els.list) return;
    if (!places.length) {
      els.list.innerHTML = "";
      if (els.empty) els.empty.hidden = false;
      return;
    }
    if (els.empty) els.empty.hidden = true;
    var html = places
      .map(function (place) {
        var cat = CATS[place.category] || { label: "", color: "#666", emoji: "📍" };
        var meta = [place.district, place.address].filter(Boolean).join(" · ");
        return (
          '<button type="button" class="place-item" data-slug="' +
          esc(place.slug) +
          '">' +
          '<span class="place-cat" style="color:' +
          esc(cat.color) +
          '">' +
          esc(cat.emoji + " " + cat.label) +
          "</span>" +
          "<h3>" +
          esc(place.name) +
          "</h3>" +
          '<p class="meta-line">' +
          esc(meta) +
          "</p>" +
          '<p class="intro">' +
          esc(place.intro || "") +
          "</p>" +
          "</button>"
        );
      })
      .join("");
    els.list.innerHTML = html;
  }

  function applyFilters(fit) {
    var places = visiblePlaces();
    if (els.count) els.count.textContent = places.length + " 处";
    renderList(places);
    clearMarkers();
    addMarkers(places);
    if (fit) fitVisible(places, 14);
    if (
      state.selected &&
      !places.some(function (place) {
        return place.slug === state.selected;
      })
    ) {
      state.selected = null;
    }
  }

  function switchBase(base) {
    if (!map || !layers || base === state.base) return;
    var center = map.getCenter();
    var wgs =
      state.base === "amap"
        ? gcjToWgs(center.lat, center.lng)
        : [center.lat, center.lng];
    var zoom = map.getZoom();
    state.base = base;
    layers.amap.remove();
    layers.osm.remove();
    layers[base].addTo(map);
    var target = base === "amap" ? wgsToGcj(wgs[0], wgs[1]) : wgs;
    map.setView(target, zoom, { animate: false });
    clearMarkers();
    addMarkers(visiblePlaces());
    document.querySelectorAll(".base-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-base") === base);
    });
  }

  function bindEvents() {
    var chips = document.querySelectorAll(".place-chip");
    Array.prototype.forEach.call(chips, function (btn) {
      btn.addEventListener("click", function () {
        Array.prototype.forEach.call(chips, function (other) {
          other.classList.remove("active");
          other.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        state.cat = btn.getAttribute("data-cat");
        applyFilters(true);
      });
    });

    if (els.search) {
      var timer = 0;
      els.search.addEventListener("input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          state.q = els.search.value;
          applyFilters(false);
        }, 120);
      });
    }

    if (els.list) {
      els.list.addEventListener("click", function (event) {
        var target = event.target;
        var item = target && target.closest ? target.closest(".place-item") : null;
        if (!item) return;
        setSelected(item.getAttribute("data-slug"), true);
      });
    }

    document.querySelectorAll(".base-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchBase(btn.getAttribute("data-base"));
      });
    });

    var coreBtn = document.getElementById("fit-core");
    var cityBtn = document.getElementById("fit-city");
    if (coreBtn) coreBtn.addEventListener("click", fitCore);
    if (cityBtn) {
      cityBtn.addEventListener("click", function () {
        fitVisible(visiblePlaces(), 13);
      });
    }
  }

  initMap();
  bindEvents();
  applyFilters(false);
  if (map) fitCore();
})();
