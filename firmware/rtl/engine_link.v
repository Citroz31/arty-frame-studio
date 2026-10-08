`timescale 1ns/1ps
// Clock-domain crossing between the control logic (UART, packets, replies,
// 100 MHz board clock) and the frame engine, which runs on the core clock
// chosen for the frame frequency. The two clocks are unrelated.
//
// Only single-bit toggles cross the boundary, each through a two-flip-flop
// synchronizer. Multi-bit values are held still while the other side samples
// them:
//  - the SEND configuration is written in the control domain with the start
//    toggle and changes again only after the engine has acknowledged it;
//  - the engine status {stop ack, start ack, busy, completed} is copied into
//    a snapshot register one core cycle before its acknowledge toggles, and
//    is not modified until the control side has taken it and asked again.
// The control side therefore always holds a coherent status, at most a few
// cycles old. A start or stop still in flight is reported as pending.
module engine_link (
    // Control domain.
    input wire ctrl_clk, ctrl_reset,
    input wire start_request,   // one cycle: SEND accepted, configuration written
    input wire stop_request,    // one cycle: STOP accepted
    output wire start_pending,  // the engine has not yet taken the last SEND
    output wire stop_pending,   // the engine has not yet taken the last STOP
    output reg engine_busy,     // last coherent status from the engine
    output reg [15:0] engine_completed,
    // Core (engine) domain.
    input wire core_clk, core_reset,
    output reg core_start,      // one core cycle, for frame_engine.start
    output reg core_stop,       // one core cycle, for frame_engine.stop
    input wire core_busy,
    input wire [15:0] core_completed
);
    reg start_toggle, stop_toggle, snap_toggle;
    reg acked_start, acked_stop;
    (* ASYNC_REG="TRUE" *) reg snap_ack_meta, snap_ack_sync;

    (* ASYNC_REG="TRUE" *) reg start_meta, start_sync, stop_meta, stop_sync;
    (* ASYNC_REG="TRUE" *) reg snap_meta, snap_sync;
    reg start_seen, stop_seen, snap_seen;
    // Toggle values the engine has acted on: updated on the edge where the
    // engine samples core_start/core_stop, so a snapshot never reports a
    // start as taken while busy still shows the state before it.
    reg start_done, stop_done;
    reg snap_ready, snap_ack;
    reg [18:0] snapshot;

    assign start_pending = start_toggle != acked_start;
    assign stop_pending = stop_toggle != acked_stop;

    always @(posedge ctrl_clk) begin
        if (ctrl_reset) begin
            start_toggle <= 0;
            stop_toggle <= 0;
            snap_toggle <= 0;
            snap_ack_meta <= 0;
            snap_ack_sync <= 0;
            acked_start <= 0;
            acked_stop <= 0;
            engine_busy <= 0;
            engine_completed <= 0;
        end else begin
            snap_ack_meta <= snap_ack;
            snap_ack_sync <= snap_ack_meta;
            if (start_request) start_toggle <= !start_toggle;
            if (stop_request) stop_toggle <= !stop_toggle;
            // The engine answered the last request: its snapshot is stable.
            if (snap_ack_sync == snap_toggle) begin
                {acked_stop, acked_start, engine_busy, engine_completed} <= snapshot;
                snap_toggle <= !snap_toggle;
            end
        end
    end

    always @(posedge core_clk) begin
        if (core_reset) begin
            start_meta <= 0;
            start_sync <= 0;
            stop_meta <= 0;
            stop_sync <= 0;
            snap_meta <= 0;
            snap_sync <= 0;
            start_seen <= 0;
            stop_seen <= 0;
            snap_seen <= 0;
            start_done <= 0;
            stop_done <= 0;
            core_start <= 0;
            core_stop <= 0;
            snap_ready <= 0;
            snap_ack <= 0;
            snapshot <= 0;
        end else begin
            start_meta <= start_toggle;
            start_sync <= start_meta;
            stop_meta <= stop_toggle;
            stop_sync <= stop_meta;
            snap_meta <= snap_toggle;
            snap_sync <= snap_meta;
            core_start <= start_sync != start_seen;
            core_stop <= stop_sync != stop_seen;
            start_seen <= start_sync;
            stop_seen <= stop_sync;
            start_done <= start_seen;
            stop_done <= stop_seen;
            snap_ready <= 0;
            if (snap_sync != snap_seen) begin
                snap_seen <= snap_sync;
                snapshot <= {stop_done, start_done, core_busy, core_completed};
                snap_ready <= 1;
            end
            if (snap_ready) snap_ack <= snap_seen;
        end
    end
endmodule
