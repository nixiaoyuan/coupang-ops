import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [input, output, preview] = process.argv.slice(2);
if (!input || !output) throw new Error('Expected workbook input JSON and XLSX output');
const data = JSON.parse(await fs.readFile(input, 'utf8'));
const wb = Workbook.create();
const overview = wb.worksheets.add('更新状态');
const details = wb.worksheets.add('接口数据');
const text = value => typeof value === 'string' && /^[=+@-]/.test(value) ? "'" + value : value;
const numeric = value => {
  if (value === undefined || value === null) return null;
  const result = Number(value);
  if (!Number.isFinite(result) || Math.abs(result) > Number.MAX_SAFE_INTEGER) throw new Error('Numeric value cannot be represented safely in Excel');
  return result;
};
const dateValue = value => value ? new Date(value.length === 10 ? value + 'T00:00:00Z' : value) : null;
const labels = {last_30d:'最近完整30天',current_7d:'最近完整7天',previous_7d:'前一个完整7天'};
const translated = {success:'更新成功',synthetic_demo:'模拟数据',auth_required:'需要重新登录或验证',fetch_failed:'取数失败',analysis_failed:'分析校验失败',stale:'上次成功数据'};
const sectionLabel = {ok:'已计算',provisional:'观察值',missing_data:'缺少数据',incomplete:'数据不完整'};

for (const sheet of [overview, details]) {
  sheet.showGridLines = false;
  sheet.getRange('A1:T3').format.font.name = 'Arial';
  sheet.getRange('A1:T3').format.font.size = 10;
  sheet.getRange('A1').format.font.size = 14;
  sheet.getRange('A1').format.font.bold = true;
}
overview.getRange('A1').values = [['Coupang 四店更新状态']];
overview.getRange('A2').values = [['更新时间（韩国时区）：' + data.state.updated_at]];
overview.getRange('A3').values = [['销售指标为平台返回口径；取消和退货扣减尚未确认。']];
details.getRange('A1').values = [['Coupang 接口返回数据']];
details.getRange('A2').values = [['来源：WING business-insight/vi-detail-search，按店铺和窗口分别展示。']];
details.getRange('A3').values = [['失败店铺保留上次成功数据并标注；金额占比仅对完整且同店、同币种、同窗口计算。']];

const headers = ['店铺','统计窗口','开始日期','结束日期','商品选项ID','商品','后台销量','后台GMV','币种','订单数','访客UV','浏览PV','点击','搜索','转化指标（原值）','履约模式','缺货标记','抓取时间（韩国时区）','更新状态','全店GMV占比'];
const records = [];
const incompleteRows = [];
for (const dataset of data.datasets) {
  for (const [windowName, window] of Object.entries(dataset.sales_windows || {})) {
    for (const item of window.rows || []) {
      const metrics = item.traffic || {};
      records.push([
        text(dataset.store), labels[windowName] || text(windowName), dateValue(window.start_date), dateValue(window.end_date),
        text(item.sku), text(item.name), numeric(item.units), numeric(item.amount), text(dataset.currency), numeric(metrics.totalOrders),
        numeric(metrics.totalUniqueVisitor), numeric(metrics.totalPageViews), numeric(metrics.srpClick), numeric(metrics.searchVolume),
        numeric(metrics.pvToOrder), text(item.registration_type || ''), item.is_oos === true ? '是' : item.is_oos === false ? '否' : '未知',
        // ISO instants are formatted into the declared business timezone before creating sortable serial dates.
        dateValue((window.captured_at || dataset.observed_at).slice(0,19) + 'Z'),
        translated[dataset.refresh_status] || text(dataset.refresh_status || 'unknown'), null,
      ]);
      if (!window.complete) incompleteRows.push(records.length + 4);
    }
  }
}
details.getRange('A4:T4').values = [headers];
const totalRows=[];
const totalRowMap=new Map();
for(const dataset of data.datasets) {
  for(const [key,window] of Object.entries(dataset.sales_windows || {})) {
    if(!window.complete) continue;
    const index=totalRows.length+5;
    totalRowMap.set(JSON.stringify([dataset.store,labels[key] || key,dataset.currency]),index);
    totalRows.push([text(dataset.store),labels[key] || text(key),dataset.currency,null]);
  }
}
if(totalRows.length && records.length) {
  const last=totalRows.length+4;
  const end=records.length+4;
  details.getRange('V4:Y4').values=[['店铺','统计窗口','币种','窗口后台GMV']];
  details.getRange(`V5:Y${last}`).values=totalRows;
  details.getRange(`Y5:Y${last}`).formulas=totalRows.map((_,index)=>{
    const row=index+5;
    return [`=SUMIFS($H$5:$H$${end},$A$5:$A$${end},V${row},$B$5:$B$${end},W${row},$I$5:$I$${end},X${row})`];
  });
  details.getRange(`Y5:Y${last}`).setNumberFormat('#,##0;(#,##0);0');
  details.getRange('V:Y').format.columnWidth=20;
  details.getRange('V4:Y4').format.font.bold=true;
  details.getRange('V4:Y4').format.fill='#E5EAF0';
}
if (records.length) {
  const end = records.length + 4;
  details.getRange(`A5:T${end}`).values = records;
  details.getRange(`A4:T${end}`).format.font.name = 'Arial';
  details.getRange(`A4:T${end}`).format.font.size = 10;
  details.getRange(`A5:T${end}`).format.verticalAlignment = 'center';
  const incomplete = new Set(incompleteRows);
  details.getRange(`T5:T${end}`).formulas = records.map((row,index) => {
    const r = index + 5;
    const total=totalRowMap.get(JSON.stringify([String(row[0]).replace(/^'/,''),row[1],row[8]]));
    return [incomplete.has(r) || !total ? '' : `=IF($Y$${total}<=0,"",H${r}/$Y$${total})`];
  });
  details.getRange(`C5:D${end}`).setNumberFormat('yyyy-mm-dd');
  details.getRange(`E5:E${end}`).setNumberFormat('@');
  details.getRange(`G5:N${end}`).setNumberFormat('#,##0;(#,##0);0');
  details.getRange(`O5:O${end}`).setNumberFormat('0.0000');
  details.getRange(`P5:Q${end}`).setNumberFormat('@');
  details.getRange(`R5:R${end}`).setNumberFormat('yyyy-mm-dd hh:mm');
  details.getRange(`T5:T${end}`).setNumberFormat('0.00%');
  details.tables.add(`A4:T${end}`, true, 'ApiResponseData');
}
details.freezePanes.freezeRows(4);
details.freezePanes.freezeColumns(2);

const summaryHeaders=['店铺','本次更新','说明','30天后台销量','30天后台GMV','币种','排名','销量下降','库存覆盖','滞销','退货观察'];
overview.getRange('A5:K5').values=[summaryHeaders];
const storeEntries = data.state.stores || [];
const summaryRows = storeEntries.map(entry => {
  const summary = data.summaries.find(item => item.store === entry.store);
  const dataset = data.datasets.find(item => item.store === entry.store);
  return [text(entry.store),translated[entry.status] || text(entry.status),text(entry.message || ''),null,null,dataset?.currency || '',
    ...['ranking','sales_drop','stock_risk','slow_stock','returns'].map(key => summary ? sectionLabel[summary[key]?.status] || '未知' : '无本次数据')];
});
if (summaryRows.length) {
  const last=summaryRows.length+5;
  overview.getRange(`A6:K${last}`).values=summaryRows;
  overview.getRange(`A5:K${last}`).format.font.name='Arial';
  overview.getRange(`A5:K${last}`).format.font.size=10;
  overview.getRange(`A6:K${last}`).format.verticalAlignment='center';
  for(let index=0;index<storeEntries.length;index++) {
    const row=index+6;
    const dataset=data.datasets.find(item=>item.store===storeEntries[index].store);
    const complete=dataset?.sales_windows?.last_30d?.complete;
    if(records.length && complete) {
      overview.getRange(`D${row}`).formulas=[[`=SUMIFS('接口数据'!$G$5:$G$${records.length+4},'接口数据'!$A$5:$A$${records.length+4},A${row},'接口数据'!$B$5:$B$${records.length+4},"最近完整30天",'接口数据'!$I$5:$I$${records.length+4},F${row})`]];
      const total=totalRowMap.get(JSON.stringify([storeEntries[index].store,'最近完整30天',dataset.currency]));
      if(total) overview.getRange(`E${row}`).formulas=[[`='接口数据'!Y${total}`]];
    }
  }
  overview.getRange(`D6:E${last}`).setNumberFormat('#,##0;(#,##0);0');
  overview.getRange(`A6:K${last}`).format.rowHeight=36;
  overview.getRange(`C6:C${last}`).format.wrapText=true;
  overview.tables.add(`A5:K${last}`,true,'RefreshStatus');
}
for(const [sheet,range] of [[overview,'A5:K5'],[details,'A4:T4']]) {
  const header=sheet.getRange(range);
  header.format.fill='#233B53';
  header.format.font.color='#FFFFFF';
  header.format.font.bold=true;
  header.format.rowHeight=34;
  header.format.wrapText=true;
  header.format.horizontalAlignment='center';
  header.format.verticalAlignment='center';
}
overview.getRange('A:A').format.columnWidth=18;
overview.getRange('B:B').format.columnWidth=23;
overview.getRange('C:C').format.columnWidth=44;
overview.getRange('D:K').format.columnWidth=16;
details.getRange('A:B').format.columnWidth=19;
details.getRange('C:D').format.columnWidth=14;
details.getRange('E:E').format.columnWidth=24;
details.getRange('F:F').format.columnWidth=48;
details.getRange('G:Q').format.columnWidth=15;
details.getRange('R:S').format.columnWidth=24;
details.getRange('T:T').format.columnWidth=18;
wb.recalculate();
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:10},maxChars:1500});
if (/"value"\s*:\s*"#(?:REF|DIV|VALUE|NAME|NUM|SPILL|CALC)/.test(errors.ndjson)) throw new Error('Workbook calculation error');
await fs.mkdir(path.dirname(output),{recursive:true});
const temp=output+'.tmp.xlsx';
await (await SpreadsheetFile.exportXlsx(wb)).save(temp);
await fs.rename(temp,output);
if(preview==='--render') {
  for(const [sheetName,range] of [['更新状态','A1:K10'],['接口数据','A1:J10']]) {
    const png=await wb.render({sheetName,range,scale:1.5,format:'png'});
    await fs.writeFile(path.join(path.dirname(output),sheetName+'.png'),new Uint8Array(await png.arrayBuffer()));
  }
}
console.log(JSON.stringify({status:'updated',rows:records.length,stores:storeEntries.length}));
