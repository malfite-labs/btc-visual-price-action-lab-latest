import {epochMilliseconds} from './time'

export function quoteIsFresh(quote:any,restStatus:string,now=Date.now()) {
 const age=now-epochMilliseconds(quote?.event_at)
 return restStatus==='PASS' && Number.isFinite(age) && age>=0 && age<=60000
}

export function shadowReviewText(review:any) {
 return Array.isArray(review) ? `第 ${review[0]} 笔复盘 ${review[1]}；候选及实际启用状态未提供` : '尚无复盘记录'
}

export function fillModelText(model:any) {
 return model==='QUOTE_SIDE_FULL_FILL_V1' ? `${model}：不按盘口数量限制完整模拟成交` : model ? `${model}（盘口规则以执行器为准）` : '接口未提供撮合模型'
}
