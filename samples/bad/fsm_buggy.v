// ============================================================================
//  fsm_buggy.v —— 状态机写法中的典型缺陷
//  GROUND TRUTH: R001@23 R003@38 R003@39 R005@45 R007@50 R008@20
// ============================================================================
module fsm_buggy (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       req,
    input  wire       ack,
    output reg        grant,
    output reg  [2:0] cnt
);

    localparam [1:0] S_IDLE = 2'd0;
    localparam [1:0] S_WAIT = 2'd1;
    localparam [1:0] S_DONE = 2'd2;

    reg [1:0] state;
    reg [1:0] nstate;
    reg [1:0] dbg_state;

    // [R001] else if 链没有终结 else：处于 S_DONE 时 nstate 无赋值 -> 推断 latch
    always @(state or req or ack) begin
        if (state == S_IDLE) begin
            if (req) nstate = S_WAIT;
        end
        else if (state == S_WAIT) begin
            if (ack) nstate = S_DONE;
        end
    end

    // [R003] 时序块内误用阻塞赋值；[R008] dbg_state 只写不读
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_IDLE;
            cnt   <= 3'd0;
        end else begin
            state     = nstate;
            cnt       = cnt + 3'd1;
            dbg_state <= state;
        end
    end

    // [R005] 块内读取 ack，但敏感列表只有 state
    always @(state) begin
        grant = (state == S_DONE) && ack;
    end

    // [R007] initial 过程块不可综合
    initial begin
        state = S_IDLE;
    end

endmodule
