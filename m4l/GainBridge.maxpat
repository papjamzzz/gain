{
	"patcher" : {
		"fileversion" : 1,
		"appversion" : { "major" : 8, "minor" : 6, "revision" : 0 },
		"rect" : [ 0, 0, 600, 500 ],
		"bglocked" : 0,
		"openinpresentation" : 1,
		"default_fontsize" : 12.0,
		"default_fontface" : 0,
		"default_fontname" : "Arial",
		"gridonopen" : 1,
		"gridsize" : [ 8.0, 8.0 ],
		"gridsnaponopen" : 1,
		"objectsnaponopen" : 1,
		"statusbarvisible" : 2,
		"toolbarvisible" : 1,
		"lefttoolbarpinned" : 0,
		"toptoolbarpinned" : 0,
		"righttoolbarpinned" : 0,
		"bottomtoolbarpinned" : 0,
		"toolbars_unpinned_last_save" : 0,
		"tallnewobj" : 0,
		"boxanimatetime" : 200,
		"enablehscroll" : 1,
		"enablevscroll" : 1,
		"devicewidth" : 600.0,
		"description" : "Gain Behavioral Mixer — Claude inside Ableton",
		"digest" : "",
		"tags" : "gain, claude, ai, behavioral",
		"style" : "",
		"subpatcher_template" : "",
		"boxes" : [
			{
				"box" : {
					"id" : "obj-1",
					"maxclass" : "node.script",
					"numinlets" : 1,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 10, 10, 120, 22 ],
					"scriptname" : "gain_bridge.js",
					"text" : "node.script gain_bridge.js"
				}
			},
			{
				"box" : {
					"id" : "obj-2",
					"maxclass" : "newobj",
					"numinlets" : 1,
					"numoutlets" : 8,
					"outlettype" : [ "", "", "", "", "", "", "", "" ],
					"patching_rect" : [ 10, 40, 200, 22 ],
					"text" : "route mode intensity depth room status response tokens"
				}
			},
			{
				"box" : {
					"id" : "obj-title",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"patching_rect" : [ 10, 10, 200, 22 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 8, 200, 24 ],
					"text" : "GAIN — Behavioral Mixer",
					"textcolor" : [ 0.0, 0.87, 0.83, 1.0 ],
					"fontsize" : 14.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-subtitle",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"patching_rect" : [ 10, 30, 300, 18 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 30, 300, 16 ],
					"text" : "Claude inside Ableton",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 10.0
				}
			},
			{
				"box" : {
					"id" : "obj-mode-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"patching_rect" : [ 10, 48, 60, 16 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 48, 60, 16 ],
					"text" : "MODE",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-btn-build",
					"maxclass" : "button",
					"numinlets" : 1,
					"numoutlets" : 1,
					"outlettype" : [ "bang" ],
					"patching_rect" : [ 10, 65, 60, 24 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 65, 60, 24 ],
					"style" : "live_toggle",
					"varname" : "btn_build"
				}
			},
			{
				"box" : {
					"id" : "obj-lbl-build",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"patching_rect" : [ 15, 67, 52, 20 ],
					"presentation" : 1,
					"presentation_rect" : [ 15, 67, 52, 20 ],
					"text" : "BUILD",
					"textcolor" : [ 0.0, 0.87, 0.83, 1.0 ],
					"fontsize" : 10.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-btn-explore",
					"maxclass" : "button",
					"numinlets" : 1,
					"numoutlets" : 1,
					"outlettype" : [ "bang" ],
					"patching_rect" : [ 80, 65, 70, 24 ],
					"presentation" : 1,
					"presentation_rect" : [ 80, 65, 70, 24 ],
					"style" : "live_toggle",
					"varname" : "btn_explore"
				}
			},
			{
				"box" : {
					"id" : "obj-lbl-explore",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"patching_rect" : [ 83, 67, 65, 20 ],
					"presentation" : 1,
					"presentation_rect" : [ 83, 67, 65, 20 ],
					"text" : "EXPLORE",
					"textcolor" : [ 0.67, 0.55, 0.98, 1.0 ],
					"fontsize" : 10.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-intensity-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 10, 100, 80, 16 ],
					"text" : "INTENSITY",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-dial-intensity",
					"maxclass" : "live.dial",
					"numinlets" : 1,
					"numoutlets" : 2,
					"outlettype" : [ "", "float" ],
					"parameter_enable" : 1,
					"patching_rect" : [ 10, 115, 48, 48 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 115, 48, 48 ],
					"saved_attribute_attributes" : {
						"valueof" : { "parameter_initial" : [ 0.6 ], "parameter_initial_enable" : 1,
							"parameter_longname" : "Intensity", "parameter_mmax" : 1.0, "parameter_mmin" : 0.0,
							"parameter_shortname" : "Intens", "parameter_type" : 0 }
					},
					"varname" : "dial_intensity"
				}
			},
			{
				"box" : {
					"id" : "obj-depth-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 70, 100, 80, 16 ],
					"text" : "DEPTH",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-dial-depth",
					"maxclass" : "live.dial",
					"numinlets" : 1,
					"numoutlets" : 2,
					"outlettype" : [ "", "float" ],
					"parameter_enable" : 1,
					"patching_rect" : [ 70, 115, 48, 48 ],
					"presentation" : 1,
					"presentation_rect" : [ 70, 115, 48, 48 ],
					"saved_attribute_attributes" : {
						"valueof" : { "parameter_initial" : [ 0.5 ], "parameter_initial_enable" : 1,
							"parameter_longname" : "Depth", "parameter_mmax" : 1.0, "parameter_mmin" : 0.0,
							"parameter_shortname" : "Depth", "parameter_type" : 0 }
					},
					"varname" : "dial_depth"
				}
			},
			{
				"box" : {
					"id" : "obj-room-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 130, 100, 80, 16 ],
					"text" : "VERBOSITY",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-dial-room",
					"maxclass" : "live.dial",
					"numinlets" : 1,
					"numoutlets" : 2,
					"outlettype" : [ "", "float" ],
					"parameter_enable" : 1,
					"patching_rect" : [ 130, 115, 48, 48 ],
					"presentation" : 1,
					"presentation_rect" : [ 130, 115, 48, 48 ],
					"saved_attribute_attributes" : {
						"valueof" : { "parameter_initial" : [ 0.4 ], "parameter_initial_enable" : 1,
							"parameter_longname" : "Verbosity", "parameter_mmax" : 1.0, "parameter_mmin" : 0.0,
							"parameter_shortname" : "Verb", "parameter_type" : 0 }
					},
					"varname" : "dial_room"
				}
			},
			{
				"box" : {
					"id" : "obj-prompt-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 10, 175, 200, 16 ],
					"text" : "ASK CLAUDE",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-prompt",
					"maxclass" : "textedit",
					"numinlets" : 1,
					"numoutlets" : 2,
					"outlettype" : [ "", "" ],
					"patching_rect" : [ 10, 192, 370, 22 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 192, 370, 22 ],
					"text" : ""
				}
			},
			{
				"box" : {
					"id" : "obj-ask-btn",
					"maxclass" : "button",
					"numinlets" : 1,
					"numoutlets" : 1,
					"outlettype" : [ "bang" ],
					"patching_rect" : [ 388, 192, 50, 22 ],
					"presentation" : 1,
					"presentation_rect" : [ 388, 192, 50, 22 ],
					"varname" : "btn_ask"
				}
			},
			{
				"box" : {
					"id" : "obj-ask-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 393, 194, 42, 18 ],
					"text" : "ASK",
					"textcolor" : [ 0.0, 0.87, 0.83, 1.0 ],
					"fontsize" : 10.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-response-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 10, 225, 200, 16 ],
					"text" : "RESPONSE",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "obj-response",
					"maxclass" : "textedit",
					"numinlets" : 1,
					"numoutlets" : 2,
					"outlettype" : [ "", "" ],
					"patching_rect" : [ 10, 242, 430, 180 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 242, 430, 180 ],
					"readonly" : 1,
					"text" : "Response will appear here..."
				}
			},
			{
				"box" : {
					"id" : "obj-status",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"patching_rect" : [ 10, 430, 430, 18 ],
					"presentation" : 1,
					"presentation_rect" : [ 10, 430, 430, 18 ],
					"text" : "status: ready",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"varname" : "status_display"
				}
			},
			{
				"box" : {
					"id" : "obj-refresh-btn",
					"maxclass" : "button",
					"numinlets" : 1,
					"numoutlets" : 1,
					"outlettype" : [ "bang" ],
					"patching_rect" : [ 450, 10, 60, 22 ],
					"presentation" : 1,
					"presentation_rect" : [ 450, 10, 60, 22 ],
					"varname" : "btn_refresh"
				}
			},
			{
				"box" : {
					"id" : "obj-refresh-lbl",
					"maxclass" : "comment",
					"numinlets" : 1,
					"numoutlets" : 0,
					"presentation" : 1,
					"presentation_rect" : [ 453, 12, 55, 18 ],
					"text" : "SYNC",
					"textcolor" : [ 0.5, 0.6, 0.7, 1.0 ],
					"fontsize" : 9.0,
					"fontface" : 1
				}
			},
			{
				"box" : {
					"id" : "msg-getstate",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 10, 80, 22 ],
					"text" : "getstate"
				}
			},
			{
				"box" : {
					"id" : "msg-build",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 40, 100, 22 ],
					"text" : "setmode BUILD"
				}
			},
			{
				"box" : {
					"id" : "msg-explore",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 310, 40, 110, 22 ],
					"text" : "setmode EXPLORE"
				}
			},
			{
				"box" : {
					"id" : "msg-intensity",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 70, 150, 22 ],
					"text" : "setfader intensity $1"
				}
			},
			{
				"box" : {
					"id" : "msg-depth",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 100, 140, 22 ],
					"text" : "setfader depth $1"
				}
			},
			{
				"box" : {
					"id" : "msg-room",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 130, 140, 22 ],
					"text" : "setfader room $1"
				}
			},
			{
				"box" : {
					"id" : "msg-ask",
					"maxclass" : "message",
					"numinlets" : 2,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 160, 200, 22 ],
					"text" : "ask $1"
				}
			},
			{
				"box" : {
					"id" : "obj-prepend-ask",
					"maxclass" : "newobj",
					"numinlets" : 1,
					"numoutlets" : 1,
					"outlettype" : [ "" ],
					"patching_rect" : [ 200, 185, 80, 22 ],
					"text" : "prepend ask"
				}
			}
		],
		"lines" : [
			{ "patchline" : { "source" : [ "obj-1", 0 ], "destination" : [ "obj-2", 0 ] } },
			{ "patchline" : { "source" : [ "obj-refresh-btn", 0 ], "destination" : [ "msg-getstate", 0 ] } },
			{ "patchline" : { "source" : [ "msg-getstate", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-btn-build", 0 ], "destination" : [ "msg-build", 0 ] } },
			{ "patchline" : { "source" : [ "msg-build", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-btn-explore", 0 ], "destination" : [ "msg-explore", 0 ] } },
			{ "patchline" : { "source" : [ "msg-explore", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-dial-intensity", 1 ], "destination" : [ "msg-intensity", 0 ] } },
			{ "patchline" : { "source" : [ "msg-intensity", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-dial-depth", 1 ], "destination" : [ "msg-depth", 0 ] } },
			{ "patchline" : { "source" : [ "msg-depth", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-dial-room", 1 ], "destination" : [ "msg-room", 0 ] } },
			{ "patchline" : { "source" : [ "msg-room", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-ask-btn", 0 ], "destination" : [ "obj-prepend-ask", 0 ] } },
			{ "patchline" : { "source" : [ "obj-prompt", 0 ], "destination" : [ "msg-ask", 0 ] } },
			{ "patchline" : { "source" : [ "obj-prepend-ask", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "msg-ask", 0 ], "destination" : [ "obj-1", 0 ] } },
			{ "patchline" : { "source" : [ "obj-2", 4 ], "destination" : [ "obj-status", 0 ] } },
			{ "patchline" : { "source" : [ "obj-2", 5 ], "destination" : [ "obj-response", 0 ] } }
		]
	}
}
