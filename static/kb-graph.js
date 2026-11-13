(function () {
  var host = document.getElementById("kb-map");
  var raw = document.getElementById("kb-graph-data");
  if (!host || !raw || typeof d3 === "undefined") {
    return;
  }
  var data;
  try {
    data = JSON.parse(raw.textContent || "{}");
  } catch (error) {
    return;
  }
  var nodes = (data.nodes || []).map(function (node) {
    return Object.assign({}, node);
  });
  if (nodes.length < 2) {
    return;
  }
  var byId = {};
  nodes.forEach(function (node) {
    byId[node.id] = node;
    node.r = node.role === "you" ? 28 : node.role === "hub" ? 20 : 8;
  });
  var links = (data.edges || [])
    .map(function (edge) {
      if (!byId[edge.from] || !byId[edge.to]) {
        return null;
      }
      return {
        source: edge.from,
        target: edge.to,
        related: !!edge.related,
      };
    })
    .filter(Boolean);

  var width = Math.max(host.clientWidth || 0, 720);
  var height = 520;
  var cx = width / 2;
  var cy = height / 2;
  var you = byId.you;
  if (you) {
    you.fx = cx;
    you.fy = cy;
  }

  var svg = d3
    .select(host)
    .append("svg")
    .attr("viewBox", "0 0 " + width + " " + height)
    .attr("role", "presentation");
  var root = svg.append("g");
  var tip = d3.select("#tip");

  svg.call(
    d3
      .zoom()
      .scaleExtent([0.55, 2.4])
      .on("zoom", function (event) {
        root.attr("transform", event.transform);
      })
  );

  var link = root
    .append("g")
    .attr("class", "kb-links")
    .selectAll("path")
    .data(links)
    .join("path")
    .attr("class", function (d) {
      return "kb-edge" + (d.related ? " related" : "");
    });

  var node = root
    .append("g")
    .selectAll("g")
    .data(nodes)
    .join("g")
    .attr("class", function (d) {
      return "kb-node kb-" + d.role + " kind-" + d.kind;
    })
    .call(
      d3
        .drag()
        .on("start", function (event, d) {
          if (!event.active) {
            simulation.alphaTarget(0.25).restart();
          }
          d.fx = d.x;
          d.fy = d.y;
        })
        .on("drag", function (event, d) {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on("end", function (event, d) {
          if (!event.active) {
            simulation.alphaTarget(0);
          }
          if (d.role !== "you") {
            d.fx = null;
            d.fy = null;
          }
        })
    );

  node
    .append("circle")
    .attr("r", function (d) {
      return d.r;
    });
  node
    .append("text")
    .attr("class", "kb-label")
    .attr("text-anchor", "middle")
    .attr("dy", function (d) {
      return d.role === "leaf" ? d.r + 12 : 4;
    })
    .text(function (d) {
      return d.label;
    });

  node.on("mouseenter", function (event, d) {
    var connected = {};
    connected[d.id] = true;
    links.forEach(function (edge) {
      var sid = edge.source.id || edge.source;
      var tid = edge.target.id || edge.target;
      if (sid === d.id || tid === d.id) {
        connected[sid] = true;
        connected[tid] = true;
      }
    });
    node.classed("dim", function (item) {
      return !connected[item.id];
    });
    link.classed("dim", function (edge) {
      var sid = edge.source.id || edge.source;
      var tid = edge.target.id || edge.target;
      return !connected[sid] || !connected[tid];
    });
    if (tip.node() && d.title) {
      tip.text(d.title).attr("hidden", null);
    }
  });
  node.on("mousemove", function (event) {
    if (!tip.node() || tip.attr("hidden") !== null) {
      return;
    }
    var x = Math.min(event.clientX + 14, window.innerWidth - tip.node().offsetWidth - 8);
    var y = Math.max(event.clientY - tip.node().offsetHeight - 12, 8);
    tip.style("left", x + "px").style("top", y + "px");
  });
  node.on("mouseleave", function () {
    node.classed("dim", false);
    link.classed("dim", false);
    if (tip.node()) {
      tip.attr("hidden", "hidden");
    }
  });
  node.on("click", function (event, d) {
    if (d.href) {
      window.location.href = d.href;
    }
  });

  function collide(d) {
    if (d.role === "you") {
      return 46;
    }
    if (d.role === "hub") {
      return 38;
    }
    return 16 + Math.min((d.label || "").length, 18) * 1.6;
  }

  function linkDistance(d) {
    var a = d.source.role || byId[d.source] && byId[d.source].role;
    var b = d.target.role || byId[d.target] && byId[d.target].role;
    if (d.related) {
      return 90;
    }
    if (a === "you" || b === "you") {
      return 150;
    }
    return 72;
  }

  var simulation = d3
    .forceSimulation(nodes)
    .force(
      "link",
      d3
        .forceLink(links)
        .id(function (d) {
          return d.id;
        })
        .distance(linkDistance)
        .strength(function (d) {
          return d.related ? 0.15 : 0.7;
        })
    )
    .force("charge", d3.forceManyBody().strength(-260))
    .force("collide", d3.forceCollide(collide).iterations(3))
    .force("center", d3.forceCenter(cx, cy))
    .force(
      "radial",
      d3
        .forceRadial(function (d) {
          if (d.role === "you") {
            return 0;
          }
          return d.role === "hub" ? 150 : 250;
        }, cx, cy)
        .strength(function (d) {
          return d.role === "leaf" ? 0.35 : 0.55;
        })
    )
    .on("tick", function () {
      nodes.forEach(function (d) {
        d.x = Math.max(d.r + 36, Math.min(width - d.r - 36, d.x));
        d.y = Math.max(d.r + 18, Math.min(height - d.r - 22, d.y));
      });
      link.attr("d", function (d) {
        var mx = (d.source.x + d.target.x) / 2;
        var my = (d.source.y + d.target.y) / 2;
        var dx = d.target.x - d.source.x;
        var dy = d.target.y - d.source.y;
        var bend = d.related ? 0.18 : 0.08;
        return (
          "M" +
          d.source.x +
          "," +
          d.source.y +
          " Q" +
          (mx - dy * bend) +
          "," +
          (my + dx * bend) +
          " " +
          d.target.x +
          "," +
          d.target.y
        );
      });
      node.attr("transform", function (d) {
        return "translate(" + d.x + "," + d.y + ")";
      });
    });
})();
