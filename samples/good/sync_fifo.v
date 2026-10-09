// ============================================================================
//  sync_fifo.v —— 参考实现：同步 FIFO，作为零告警基线样本
//  GROUND TRUTH: （无）
// ============================================================================
module sync_fifo #(
    parameter WIDTH = 8,
    parameter DEPTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             wr_en,
    input  wire             rd_en,
    input  wire [WIDTH-1:0] din,
    output reg  [WIDTH-1:0] dout,
    output reg              full,
    output reg              empty
);

    localparam AW = 4;

    reg [AW-1:0]    wptr;
    reg [AW-1:0]    rptr;
    reg [AW:0]      count;
    reg [WIDTH-1:0] mem [0:DEPTH-1];

    wire wr_ok = wr_en && !full;
    wire rd_ok = rd_en && !empty;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wptr  <= {AW{1'b0}};
            rptr  <= {AW{1'b0}};
            count <= {(AW + 1){1'b0}};
            full  <= 1'b0;
            empty <= 1'b1;
            dout  <= {WIDTH{1'b0}};
        end else begin
            if (wr_ok) begin
                mem[wptr] <= din;
                wptr      <= wptr + 1'b1;
            end
            if (rd_ok) begin
                dout <= mem[rptr];
                rptr <= rptr + 1'b1;
            end
            count <= count + (wr_ok ? 1'b1 : 1'b0) - (rd_ok ? 1'b1 : 1'b0);
            full  <= (count == DEPTH - 1);
            empty <= (count == 0);
        end
    end

endmodule
