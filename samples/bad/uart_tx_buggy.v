`timescale 1ns/1ps
// ============================================================================
//  uart_tx_buggy.v  —— 教学用例：在真实 UART 发送模块中植入 8 类典型 RTL 缺陷
//
//  GROUND TRUTH: R001@41 R002@60 R003@56 R004@50 R004@70 R005@65 R006@34 R007@83 R007@84 R007@85 R008@17 R008@33 R008@36
//
//  该文件同时是 RTL-Sentinel 评测集的一条样本：期望规则引擎恰好命中上述 8 条规则，
//  命中任何未标注的规则即计为一次误报。
// ============================================================================
module uart_tx_buggy #(
    parameter CLK_FREQ = 50_000_000,
    parameter BAUD     = 115200
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       tx_start,
    input  wire [7:0] tx_data,
    output reg        tx,
    output reg        busy
);

    localparam [15:0] BAUD_DIV = CLK_FREQ / BAUD;
    localparam [1:0]  IDLE  = 2'd0;
    localparam [1:0]  START = 2'd1;
    localparam [1:0]  DATA  = 2'd2;
    localparam [1:0]  STOP  = 2'd3;

    reg  [1:0]  state;
    reg  [1:0]  next_state;
    reg  [15:0] baud_cnt;
    reg  [2:0]  bit_idx;
    reg  [7:0]  shreg;
    reg  [3:0]  err_cnt;
    reg         cdc_flag;
    wire        tick;
    wire        dbg_pulse;

    assign tick = (baud_cnt == BAUD_DIV - 16'd1);

    // [R001] 组合逻辑缺省分支：case 无 default 且无块首默认赋值 -> 推断出锁存器
    always @(state or tx_start) begin
        case (state)
            IDLE:  if (tx_start) next_state = START;
            START: next_state = DATA;
            DATA:  next_state = STOP;
        endcase
    end

    // [R003] 时序逻辑误用阻塞赋值；[R002] 4 位寄存器被 8 位常量截断
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= IDLE;
            baud_cnt <= 16'd0;
            busy     <= 1'b0;
        end else begin
            state = next_state;
            if (state == DATA) begin
                baud_cnt <= baud_cnt + 16'd1;
            end
            err_cnt <= 8'hFF;
        end
    end

    // [R005] 敏感列表不完整：块内读取 bit_idx，但列表只有 shreg
    always @(shreg) begin
        tx = shreg[bit_idx];
    end

    // [R004] 同一网络被两条连续赋值驱动
    assign dbg_pulse = tick;
    assign dbg_pulse = tx_start;

    // [R006] 跨时钟域：cdc_flag 由 clk 域写入，却被 clk_slow 域直接采样
    always @(posedge clk) begin
        cdc_flag <= tx_start;
    end

    always @(posedge clk_slow) begin
        busy <= cdc_flag;
    end

    // [R007] 含不可综合结构（initial / 延时 / 系统任务）
    initial begin
        $display("uart_tx_buggy start");
        #100;
    end

endmodule
