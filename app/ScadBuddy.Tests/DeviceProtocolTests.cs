using System.Text.Json.Nodes;
using Xunit;

namespace ScadBuddy.Tests;

public class DeviceProtocolTests
{
	const string Device = "0f6a2b1c-0000-4000-8000-000000000001";
	const string Other = "0f6a2b1c-0000-4000-8000-000000000002";
	const string Call = "5d1e7c2a-0000-4000-8000-0000000000aa";
	static readonly string Token = "sbdev_" + new string('A', 43);
	static readonly Uri Claim = new("https://sb.example/api/v1/devices/pairings/AAAAAAAAAAAAAAAAAAAAAA/claim");

	static JsonObject Obj(string json) => JsonNode.Parse(json)!.AsObject();

	[Fact]
	public void TheClaimAnnouncesTheFourMethodsOnly()
	{
		var body = DeviceProtocol.ClaimBody(new string('n', 150), " Google Pixel 9 ", "1.0");
		Assert.Equal(["camera.capture", "code.scan", "nfc.read", "nfc.write"], body["capabilities"]!.AsArray().Select(c => c!.GetValue<string>()));
		Assert.Equal(100, body["name"]!.GetValue<string>().Length);
		Assert.Equal("Google Pixel 9", body["model"]!.GetValue<string>());
		Assert.Equal(["name", "model", "app_version", "capabilities"], body.Select(p => p.Key));
	}

	[Fact]
	public void AClaimAnswerGivesThePairing()
	{
		var p = DeviceProtocol.PairingOf($$"""{"device_id":"{{Device}}","token":"{{Token}}","expires_at":"2026-11-27T13:00:00Z","hard_cap_at":"2026-12-27T12:00:00Z"}""", Claim);
		Assert.NotNull(p);
		Assert.Equal("https://sb.example", p.Origin);
		Assert.Equal(Device, p.DeviceId);
		Assert.Equal(Token, p.Token);
		Assert.Equal(new DateTimeOffset(2026, 12, 27, 12, 0, 0, TimeSpan.Zero), p.HardCapAt);
		Assert.Equal(p, DeviceProtocol.FromStored(DeviceProtocol.Stored(p)));
	}

	[Theory]
	[InlineData("""{"device_id":"x","token":"sbdev_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","hard_cap_at":"2026-12-27T12:00:00Z"}""")]
	[InlineData("""{"device_id":"0f6a2b1c-0000-4000-8000-000000000001","token":"other","hard_cap_at":"2026-12-27T12:00:00Z"}""")]
	[InlineData("""{"device_id":"0f6a2b1c-0000-4000-8000-000000000001","token":"sbdev_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}""")]
	[InlineData("""{"device_id":1}""")]
	[InlineData("[]")]
	[InlineData("not json")]
	public void AnAnswerThatIsNoPairingIsRefused(string answer) => Assert.Null(DeviceProtocol.PairingOf(answer, Claim));

	[Theory]
	[InlineData(null)]
	[InlineData("")]
	[InlineData("{}")]
	[InlineData("""{"origin":"http://sb.example","device_id":"0f6a2b1c-0000-4000-8000-000000000001","token":"sbdev_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","hard_cap_at":"2026-12-27T12:00:00Z"}""")]
	public void AStoredValueThatIsNoPairingIsNone(string? stored) => Assert.Null(DeviceProtocol.FromStored(stored));

	[Fact]
	public void TheSocketIsOnTheDevicesOrigin()
	{
		Assert.Equal("wss://sb.example:8443/api/v1/ws", DeviceProtocol.SocketOf("https://sb.example:8443").ToString());
		Assert.Equal($$"""{"type":"subscribe","topics":["device:{{Device}}"]}""", DeviceProtocol.Subscribe(Device));
	}

	static string Requested(string device, string method = "nfc.read", string args = "{}") =>
		$$$"""{"type":"event","id":"7","kind":"device.call_requested","topics":["device:{{{device}}}"],"data":{"call":"{{{Call}}}","device":"{{{device}}}","method":"{{{method}}}","args":{{{args}}},"deadline":"2026-11-27T12:01:00Z"}}""";

	[Fact]
	public void ACallForThisDeviceIsACallRequest()
	{
		var call = Assert.IsType<CallRequest>(DeviceProtocol.Parse(Requested(Device, "nfc.write", """{"records":[{"recordType":"empty"}]}"""), Device));
		Assert.Equal(Call, call.Call);
		Assert.Equal("nfc.write", call.Method);
		Assert.Equal(new DateTimeOffset(2026, 11, 27, 12, 1, 0, TimeSpan.Zero), call.Deadline);
		Assert.Equal("""{"records":[{"recordType":"empty"}]}""", call.Args.ToJsonString());
	}

	[Theory]
	[InlineData("location.get")]
	[InlineData("nfc.format")]
	public void ACallOfAnotherMethodIsIgnored(string method) =>
		Assert.Same(Frame.Ignored, DeviceProtocol.Parse(Requested(Device, method), Device));

	[Fact]
	public void ACallForAnotherDeviceIsIgnored() =>
		Assert.Same(Frame.Ignored, DeviceProtocol.Parse(Requested(Other), Device));

	[Theory]
	[InlineData("""{"type":"subscribed","topics":["device:x"]}""")]
	[InlineData("""{"type":"event","kind":"device.call_requested","data":{"call":1}}""")]
	[InlineData("""{"type":"event","kind":"device.call_requested","data":"x"}""")]
	[InlineData("""{"type":7}""")]
	[InlineData("[1]")]
	[InlineData("{")]
	public void AnotherFrameIsIgnored(string frame) => Assert.Same(Frame.Ignored, DeviceProtocol.Parse(frame, Device));

	[Fact]
	public void APingIsAPing()
	{
		Assert.Same(Frame.Ping, DeviceProtocol.Parse("""{"type":"ping"}""", Device));
		Assert.NotEqual(Frame.Ping, Frame.Ignored);
	}

	[Theory]
	[InlineData("device.call_done")]
	[InlineData("device.call_failed")]
	public void TheCallsEndIsACallEnded(string kind)
	{
		var frame = $$$"""{"type":"event","id":"9","kind":"{{{kind}}}","topics":["device:{{{Device}}}"],"data":{"call":"{{{Call}}}","device":"{{{Device}}}","outcome":"timeout"}}""";
		Assert.Equal(new CallEnded(Call), DeviceProtocol.Parse(frame, Device));
	}

	[Fact]
	public void TheEventsHaveTheKindsAndFieldsCoreTakes()
	{
		Assert.Equal("""{"kind":"tag_found","data":{"uid":"9CC69CE3","tech":"mifare_classic","blocks":48}}""",
			DeviceProtocol.TagFound("9CC69CE3", "mifare_classic", 48, 0).ToJsonString());
		Assert.Equal("""{"kind":"tag_found","data":{"uid":"9CC69CE3","tech":"mifare_classic","blocks":48,"resume_from":8}}""",
			DeviceProtocol.TagFound("9CC69CE3", "mifare_classic", 48, 8).ToJsonString());
		Assert.Equal("""{"kind":"tag_progress","data":{"read":16,"of":48}}""", DeviceProtocol.TagProgress(16, 48).ToJsonString());
		var lost = DeviceProtocol.TagLost(8, "sector 2: IOException: " + new string('x', 300));
		Assert.Equal("tag_lost", lost["kind"]!.GetValue<string>());
		Assert.Equal(DeviceProtocol.ErrorMax, lost["data"]!["error"]!.GetValue<string>().Length);
		// Core sets these itself and refuses an event that names one (devices calls.go `reserved`).
		string[] reserved = ["id", "at", "kind", "topics", "retention_seconds", "call", "device"];
		foreach (var e in new[] { DeviceProtocol.TagFound("AA", "ndef", 0, 0, "tag_dump-x"), DeviceProtocol.TagProgress(1, 2), lost })
			Assert.Empty(e["data"]!.AsObject().Select(p => p.Key).Intersect(reserved));
	}

	[Fact]
	public void EventsGoInOrderTwentyAtATime()
	{
		var events = Enumerable.Range(0, 45).Select(i => DeviceProtocol.TagProgress(i, 45)).ToList();
		var bodies = DeviceProtocol.EventBodies(events).ToList();
		Assert.Equal([20, 20, 5], bodies.Select(b => b["events"]!.AsArray().Count));
		Assert.Equal(Enumerable.Range(0, 45), bodies.SelectMany(b => b["events"]!.AsArray()).Select(e => e!["data"]!["read"]!.GetValue<int>()));
	}

	[Theory]
	[InlineData(3, true)]
	[InlineData(7, true)]
	[InlineData(63, true)]
	[InlineData(127, true)]
	[InlineData(143, true)]
	[InlineData(255, true)]
	[InlineData(0, false)]
	[InlineData(4, false)]
	[InlineData(131, false)]
	public void TheTrailersAreCoresTrailers(int block, bool trailer) => Assert.Equal(trailer, DeviceProtocol.IsTrailer(block));

	[Fact]
	public void ATagsDataBlocksLeaveOutItsTrailers()
	{
		Assert.Equal(48, DeviceProtocol.DataBlocks(64));
		Assert.Equal(216, DeviceProtocol.DataBlocks(256));
		Assert.Equal([2, 4, 5, 6, 8], DeviceProtocol.MissingBlocks(12, [0, 1, 9, 10]));
		Assert.Empty(DeviceProtocol.MissingBlocks(8, [0, 1, 2, 4, 5, 6]));
	}

	[Fact]
	public void ADumpHoldsNoKeyTrailer()
	{
		// The 1K spool of docs/superpowers/specs/2026-11-26-bambu-tag-scan-design.md, read in full:
		// 64 blocks, of which 16 are trailers that must never leave the phone.
		var all = Enumerable.Range(0, 64).Select(i => (i, i.ToString("x2") + new string('0', 30))).Reverse();
		var dump = Obj(DeviceProtocol.Dump("9CC69CE3", "mifare_classic", all, new JsonArray()));
		Assert.Equal(["uid", "tech", "blocks", "records"], dump.Select(p => p.Key));
		var blocks = dump["blocks"]!.AsArray();
		Assert.Equal(48, blocks.Count);
		Assert.DoesNotContain(blocks, b => DeviceProtocol.IsTrailer(b!["index"]!.GetValue<int>()));
		Assert.Equal(Enumerable.Range(0, 64).Where(i => !DeviceProtocol.IsTrailer(i)), blocks.Select(b => b!["index"]!.GetValue<int>()));
		Assert.Equal(["index", "data_hex"], blocks[0]!.AsObject().Select(p => p.Key));
	}

	[Fact]
	public void ADumpKeepsTheNdefRecords()
	{
		var records = new JsonArray(Obj("""{"recordType":"url","dataHex":"68747470"}"""));
		var dump = Obj(DeviceProtocol.Dump("04A1B2C3D4E5F6", "ndef", [], records));
		Assert.Empty(dump["blocks"]!.AsArray());
		Assert.Equal("""[{"recordType":"url","dataHex":"68747470"}]""", dump["records"]!.ToJsonString());
	}

	[Fact]
	public void TheResultsHaveTheShapesCoreTakes()
	{
		Assert.Equal("""{"outcome":"done","result":{"read":{"blob_key":"tag_dump-k","uid":"9CC69CE3","tech":"mifare_classic","complete":true,"missing_blocks":[]}}}""",
			DeviceProtocol.NfcResult("tag_dump-k", "9CC69CE3", "mifare_classic", []).ToJsonString());
		Assert.False(DeviceProtocol.NfcResult("k", "9CC69CE3", "mifare_classic", [40, 41])["result"]!["read"]!["complete"]!.GetValue<bool>());
		Assert.Equal("""{"outcome":"done","result":{"read":{"blob_key":"scan_image-k","media_type":"image/jpeg"}}}""",
			DeviceProtocol.PhotoResult("scan_image-k", "image/jpeg").ToJsonString());
		var failed = DeviceProtocol.Failed("no_tag: " + new string('x', 300));
		Assert.Equal("error", failed["outcome"]!.GetValue<string>());
		Assert.Equal(DeviceProtocol.ErrorMax, failed["error"]!.GetValue<string>().Length);
	}

	[Fact]
	public void AReadIsSentUnlessItsNdefReadFailed()
	{
		Assert.Null(DeviceProtocol.UnsentRead(classic: false, lostError: null));
		Assert.Null(DeviceProtocol.UnsentRead(classic: true, lostError: null));
		// A MIFARE read cut short is sent: its missing_blocks say it is partial.
		Assert.Null(DeviceProtocol.UnsentRead(classic: true, lostError: "sector 2: IOException"));
		// An NDEF read that failed has no records: it is no complete read of an empty tag.
		Assert.Equal("tag_lost", DeviceProtocol.CodeOf(DeviceProtocol.UnsentRead(classic: false, lostError: "ndef: IOException")!));
	}

	[Theory]
	[InlineData(DeviceProtocol.WriteTap.None, "no_tag")]
	[InlineData(DeviceProtocol.WriteTap.Classic, "bambu_tag")]
	[InlineData(DeviceProtocol.WriteTap.Lost, "tag_lost")]
	[InlineData(DeviceProtocol.WriteTap.Written, "no_read_back")]
	public void AWriteAtItsDeadlineSaysWhatItsLastTapDid(DeviceProtocol.WriteTap last, string code) =>
		Assert.Equal(code, DeviceProtocol.CodeOf(DeviceProtocol.WriteTimedOut(last)));

	[Theory]
	[InlineData("no_tag: no tag was tapped", "no_tag")]
	[InlineData("bambu_tag: this is a MIFARE Classic tag", "bambu_tag")]
	[InlineData("sector 2: IOException: 9CC69CE3", "error")]
	[InlineData("Upload: x", "error")]
	public void TelemetryGetsOnlyTheErrorsCode(string error, string code) => Assert.Equal(code, DeviceProtocol.CodeOf(error));

	[Theory]
	[InlineData("""{"records":[{"recordType":"text","text":"PLA","lang":"en","encoding":"utf-8"}]}""")]
	[InlineData("""{"records":[{"recordType":"url","text":"https://sb.example/s/1"}]}""")]
	[InlineData("""{"records":[{"recordType":"absolute-url","text":"https://sb.example/"}]}""")]
	[InlineData("""{"records":[{"recordType":"mime","mediaType":"application/json","hex":"7b7d"}]}""")]
	[InlineData("""{"records":[{"recordType":"example.com:spool","text":"x"},{"recordType":"empty"}]}""")]
	public void CoresParsedRecordsAreWritten(string args)
	{
		var records = DeviceProtocol.WriteRecords(Obj(args), out var error);
		Assert.Null(error);
		Assert.Equal(Obj(args)["records"]!.ToJsonString(), records!.ToJsonString());
	}

	[Theory]
	[InlineData("""{}""")]
	[InlineData("""{"records":[]}""")]
	[InlineData("""{"records":[{"recordType":"empty"}],"sector":1}""")]
	[InlineData("""{"records":[{"recordType":"smart-poster","hex":"00"}]}""")]
	[InlineData("""{"records":[{"recordType":":T","hex":"00"}]}""")]
	[InlineData("""{"records":[{"recordType":"unknown","hex":"00"}]}""")]
	[InlineData("""{"records":[{"recordType":"text","text":"x","id":"1"}]}""")]
	[InlineData("""{"records":[{"recordType":"text"}]}""")]
	[InlineData("""{"records":[{"recordType":"text","text":1}]}""")]
	[InlineData("""{"records":[{"recordType":"text","text":"x","encoding":"latin1"}]}""")]
	[InlineData("""{"records":[{"recordType":"mime","mediaType":"a/b","text":"x","hex":"00"}]}""")]
	[InlineData("""{"records":[{"recordType":"mime","text":"x"}]}""")]
	[InlineData("""{"records":[{"recordType":"example.com:spool","hex":"0A"}]}""")]
	[InlineData("""{"records":[{"recordType":"url","text":"not a uri"}]}""")]
	[InlineData("""{"records":["text"]}""")]
	public void AnyOtherRecordIsRefused(string args)
	{
		Assert.Null(DeviceProtocol.WriteRecords(Obj(args), out var error));
		Assert.StartsWith("bad_args: ", error);
	}
}
