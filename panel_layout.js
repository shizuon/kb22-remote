'use strict';

// Reuse the registered controls and their write/readback handling. This only
// changes presentation; moving a route does not change any hardware setting.
function refineGameRouting() {
  const container = document.querySelector('#page-mixer > .cards');
  if (!container || document.getElementById('game-routing-matrix')) return;

  const sourceRows = [
    ['in1', 'IN 1'], ['in2', 'IN 2'], ['otg', 'OTG IN'],
    ['pb12', 'Playback 1/2'], ['pb34', 'Playback 3/4'], ['pb56', 'Playback 5/6']
  ];
  const destinations = [
    ['hp', '耳机 / 线路'], ['otg', 'OTG OUT'], ['recording', '录音']
  ];
  const movedControls = [];
  const originalCards = new Set();
  const table = el('table', {class:'game-route-table', 'aria-label':'电竞信号路由'});
  table.append(el('thead', {}, el('tr', {},
    el('th', {scope:'col', text:'信号源'}),
    ...destinations.map(([, label]) => el('th', {scope:'col', text:label}))
  )));
  const body = el('tbody');
  for (const [source, label] of sourceRows) {
    const line = el('tr', {}, el('th', {scope:'row', text:label}));
    for (const [destination, destinationLabel] of destinations) {
      const id = 'game.route.' + source + '.' + destination;
      const row = container.querySelector('[data-control="' + id + '"]');
      const cell = el('td');
      if (row) {
        const card = row.closest('.card');
        if (card) originalCards.add(card);
        row.classList.add('game-route-control');
        const widget = row.querySelector('[role="switch"]');
        if (widget) {
          widget.setAttribute('aria-label', label + ' 发送至' + destinationLabel);
          widget.title = label + ' → ' + destinationLabel;
        }
        cell.append(row);
        movedControls.push(byId.get(id));
      } else {
        cell.append(el('span', {class:'game-route-unavailable', text:'—',
          'aria-label':label + ' 不支持发送至' + destinationLabel,
          title:label + ' 不支持发送至' + destinationLabel}));
      }
      line.append(cell);
    }
    body.append(line);
  }
  if (!movedControls.length) return;
  table.append(body);
  const matrix = el('section', {id:'game-routing-matrix', class:'card full game-only game-routing-matrix', hidden:true},
    el('div', {class:'card-head'}, el('h2', {text:'信号路由'}),
      el('span', {class:'game-route-key', text:'亮起即发送'})), table);
  container.prepend(matrix);
  groupViews.push({card:matrix, controls:movedControls});

  // Route-only cards now have no content. Playback cards retain volume and
  // mute; normal mode filtering continues to use the existing group registry.
  for (const card of originalCards) {
    const remaining = [...card.querySelectorAll('[data-control]')]
      .map(row => byId.get(row.dataset.control)).filter(Boolean);
    const group = groupViews.find(view => view.card === card);
    if (group) group.controls = remaining;
    if (!remaining.length) card.hidden = true;
    else card.classList.add('game-playback-level');
  }
}
