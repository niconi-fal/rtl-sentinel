// ============================================================================
//  comb_nonblocking.v —— 组合逻辑块内误用非阻塞赋值
//  GROUND TRUTH: R011@15
//
//  组合块里用 `<=`：仿真中要等到当前时间片末才生效，而综合结果是纯组合逻辑 ——
//  两者行为不一致。对应 Verible 的 always-comb-blocking。
// ============================================================================
module comb_nonblocking (
    input  wire       a,
    input  wire       b,
    output reg        y
);

    always @(*) begin
        y <= a & b;
    end

endmodule
