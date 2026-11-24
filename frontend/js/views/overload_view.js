export const overloadViewTemplate = `
<section class="panel">
  <h2>超重旁路</h2>
  <p class="hint">开放挂起驶向旁路；闭环按甲路径同事务重裁毛重并写正式裁定，重裁仍超载整笔回滚拒收；影子裁定当班禁用。</p>
  <div class="form-row">
    <div><label>过磅票</label>
      <select v-model.number="overloadOpenForm.ticket_id">
        <option v-for="t in adjudicableTickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>旁路备注</label><input v-model="overloadOpenForm.side_lane_note"/></div>
  </div>
  <div class="actions">
    <button @click="openOverload">人工挂起</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead><tr><th>键</th><th>票号</th><th>超重</th><th>阈值</th><th>正式毛重</th><th>裁定口径</th><th>状态</th><th>闭环复磅毛重(t)</th><th></th></tr></thead>
    <tbody>
      <tr v-for="h in overloads" :key="h.id">
        <td class="mono">{{ h.hold_key }}</td>
        <td>{{ h.ticket_code }}</td>
        <td>{{ h.overweight_t }}t</td>
        <td>{{ h.threshold_t }}t</td>
        <td>{{ fmtT(h.gross_t) }}</td>
        <td><span class="badge" :class="h.ruling_scope==='正式' ? 'pass' : 'muted'">{{ h.ruling_scope || '未裁' }}</span></td>
        <td><span class="badge" :class="h.status==='开放'?'fail':'pass'">{{ h.status }}</span></td>
        <td>
          <input v-if="h.status==='开放'" v-model.number="closeForms[h.id]"
                 type="number" step="0.1" placeholder="复磅毛重，须≤阈值" style="width:9em"/>
        </td>
        <td>
          <button v-if="h.status==='开放'" class="mini-btn" @click="closeOverload(h)">闭环重裁</button>
        </td>
      </tr>
    </tbody>
  </table>
  <p class="hint">口径锁死：仅正式裁定可晋升放行；无复磅毛重或影子口径按偏航拒写并留审计痕。</p>
</section>
`;
