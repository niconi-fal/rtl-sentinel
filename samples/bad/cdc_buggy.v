// ============================================================================
//  cdc_buggy.v —— 跨时钟域设计中的典型缺陷
//  GROUND TRUTH: R004@30 R006@14 R008@17
// ============================================================================
module cdc_buggy (
    input  wire       wr_clk,
    input  wire       rd_clk,
    input  wire [7:0] wdata,
    input  wire       wen,
    output reg        empty,
    output reg  [7:0] rdata
);

    reg  [7:0] mem;
    reg        wptr;
    reg        rptr;
    wire       sync_ptr;

    always @(posedge wr_clk) begin
        if (wen) mem <= wdata;
    end

    // [R006] mem 与 wptr 由 wr_clk 域产生，却在 rd_clk 域被直接采样
    always @(posedge rd_clk) begin
        rdata <= mem;
        empty <= ~wptr;
    end

    // [R004] sync_ptr 被两条连续赋值同时驱动
    assign sync_ptr = wptr;
    assign sync_ptr = rptr;

endmodule
