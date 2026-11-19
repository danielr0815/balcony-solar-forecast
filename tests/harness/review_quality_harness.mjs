import assert from 'node:assert/strict';
import { installDOM } from './card_dom.mjs';
const classes = installDOM();
await import(new URL('../../custom_components/balcony_solar_forecast/frontend/power_history_card.js', import.meta.url));
const Power = classes.get('balcony-power-history-card');
const card = new Power();
card._render = () => {};
card.setConfig({ total_sensor: 'sensor.total', forecast_sensor: 'sensor.forecast' });
card._hass = { config: {time_zone: 'UTC'}, states: {}, connection: {} };
const day = card._calendar().key(new Date());
const hour = `${day}T10:00:00+00:00`;
const unavailable = {state: 'unavailable', attributes: {wh_period: {[hour]: 100}}};
card._recomputeForecast(unavailable);
assert.equal(card._dayForecast, null, 'unavailable old attributes cannot be live');
card._hass.states['sensor.forecast'] = unavailable;
assert.equal(card._liveForecastTotal(card._hass), null);
const zeroHour = Object.fromEntries(["00", "15", "30", "45"].map(min => [`${day}T10:${min}:00+00:00`, 0]));
card._recomputeForecast({state:'0', attributes:{wh_period:zeroHour}});
assert.equal(card._dayForecast[10], 0, 'measured forecast zero is valid');
assert.equal(card._dayForecast[11], null, 'missing forecast hour is not zero');
card._ingestDay({'sensor.p':[{start: hour, mean: 0}]}, ['sensor.p']);
assert.equal(card._dayBars['sensor.p'][10], 0);
assert.equal(card._dayBars['sensor.p'][11], null, 'missing actual hour is not zero');
card._modules = [{id:'sensor.p',name:'P',color:'red'}];
const rows = card._readoutRows({mode:'day',hours:card._dayHours(),modules:card._modules,
  bars:card._dayBars,forecast:card._dayForecast,t:{total:'Total',forecast:'Forecast'}},11);
assert.equal(rows.find(r=>r.kind==='total').val,null,'a missing site total cannot become zero');
card._hass.states['sensor.forecast'] = {state:'1',attributes:{quantile_readiness:{enabled:true,
  weather_status:'cached',dated_days:21,by_day:{[day]:{state:'trained',trained_slots:4,positive_slots:4}}}}};
const labels = {learningTrained:'Learned',evidenceDays:'days',learningCached:'Cached weather'};
assert.ok(card._learningNote(labels).textContent.includes('Cached weather'),
  'a learned band must still disclose cached weather');
card._hass.states['sensor.forecast'].attributes.quantile_readiness.weather_status = 'fresh';
assert.ok(!card._learningNote(labels).textContent.includes('Cached weather'));
console.log('PASS unavailable, real zero, missing forecast and actual labels');

for (const bad of [true, false, '', ' ', -1, null, {}, Infinity]) {
  card._recomputeForecast({state:'1', attributes:{wh_period:{...zeroHour, [hour]:bad}}});
  assert.equal(card._dayForecast, null, 'corrupt forecast labels stay missing');
  card._ingestDay({'sensor.p':[{start:hour,mean:bad}]}, ['sensor.p']);
  assert.equal(card._dayBars['sensor.p'][10], null, 'corrupt actual labels stay missing');
}
card._config.entry_id = 'synthetic';
for (const daily_wh of [undefined, null, true, '', -1]) {
  card._hass.callWS = async () => ({response:{result:{available:true,hourly_wh:{[hour]:200}, daily_coverage:"complete",daily_wh}}});
  assert.deepEqual(await card._issuedTotal(card._hass, day), {status:'missing',value:null},
    'empty or damaged archives cannot become complete daily totals');
}
card._hass.callWS = async () => ({response:{result:{available:true,hourly_wh:{[hour]:0},daily_coverage:"complete",daily_wh:0}}});
assert.deepEqual(await card._issuedTotal(card._hass, day), {status:'available',value:0});
console.log('PASS invalid numeric labels and archive totals');
