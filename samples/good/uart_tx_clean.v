`timescale 1ns/1ps
// ============================================================================
//  uart_tx_clean.v  —— 参考实现：同一功能的工业写法，作为零告警基线
//
//  GROUND TRUTH: （无）
//
//  该样本用于度量误报率。任何在此文件上产生的告警都计入 false positive。
// ============================================================================
module uart_tx #(
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
    wire        tick;

    assign tick = (baud_cnt == BAUD_DIV - 16'd1);

    // 组合 next-state：块首默认赋值 + case 具 default + @* 完整敏感列表
    always @* begin
        next_state = state;
        case (state)
            IDLE:    if (tx_start) next_state = START;
            START:   next_state = DATA;
            DATA:    if (bit_idx == 3'd7) next_state = STOP;
            default: next_state = IDLE;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= IDLE;
            baud_cnt <= 16'd0;
            bit_idx  <= 3'd0;
            shreg    <= 8'd0;
            tx       <= 1'b1;
            busy     <= 1'b0;
        end else begin
            state <= next_state;
            if (tick) begin
                baud_cnt <= 16'd0;
            end else begin
                baud_cnt <= baud_cnt + 16'd1;
            end
            case (next_state)
                DATA: begin
                    shreg <= (bit_idx == 3'd0) ? tx_data : {1'b1, shreg[7:1]};
                    tx    <= shreg[0];
                end
                default: begin
                    tx <= 1'b1;
                end
            endcase
            bit_idx <= (tick && (next_state == DATA)) ? (bit_idx + 3'd1) : bit_idx;
            busy    <= (next_state != IDLE);
        end
    end

endmodule
