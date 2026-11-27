using System.Text;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Android.Nfc;
using Android.Nfc.Tech;

namespace ScadBuddy;

/// <summary>
/// The four methods a paired phone answers (#8244): <c>nfc.read</c>, <c>nfc.write</c>,
/// <c>code.scan</c> and <c>camera.capture</c>. There are no others. Each answers its result body
/// (<see cref="DeviceProtocol.NfcResult"/>, <see cref="DeviceProtocol.PhotoResult"/> or
/// <see cref="DeviceProtocol.Failed"/>); bytes go up as one blob first, never in the result.
/// </summary>
static class DeviceMethods
{
	public static Task<JsonObject> Run(DeviceCall call) => call.Method switch
	{
		"nfc.read" => NfcRead(call),
		"nfc.write" => NfcWrite(call),
		"code.scan" => Photo(call, "Photo of the code"),
		"camera.capture" => Photo(call, "Photo for ScadBuddy"),
		_ => Task.FromResult(DeviceProtocol.Failed("unknown_method: the app answers nfc.read, nfc.write, code.scan and camera.capture")),
	};

	/// <summary>
	/// Reads the next tag with resume: a MIFARE Classic read that stops partway sends tag_lost and
	/// keeps its sectors, and the next tap reads only the rest. When the read ends, or at the
	/// deadline, the dump goes up (no key trailers) and the result names it.
	/// </summary>
	static async Task<JsonObject> NfcRead(DeviceCall call)
	{
		if (call.Args.Count != 0) return DeviceProtocol.Failed("bad_args: nfc.read takes no args");
		var taps = Taps();
		Action<Tag> onTap = tag => taps.Writer.TryWrite(tag);
		await using var screen = await CallScreen.Show("Read a tag", "Tap the tag on the back of the phone.", call);
		var progress = new CallProgress(call, screen);
		NfcBridge.Instance.StartCall(onTap);
		try
		{
			(Tag Tag, JsonObject Reading)? last = null;
			while (true)
			{
				Tag tag;
				try { tag = await taps.Reader.ReadAsync(call.Token); }
				catch (OperationCanceledException) { break; }
				progress.LostError = null;
				var reading = await Task.Run(() => NfcBridge.ReadTag(tag, progress));
				last = (tag, reading);
				if (progress.LostError is null) break;
			}
			if (call.CancelledByPerson) return DeviceProtocol.Failed("cancelled: the person stopped the read on the phone");
			if (last is not { } done) return DeviceProtocol.Failed("no_tag: no tag was tapped before the deadline");
			if (DeviceProtocol.UnsentRead(NfcBridge.IsClassic(done.Tag), progress.LostError) is { } unsent) return DeviceProtocol.Failed(unsent);
			screen.Say("Sending the read.");
			var (key, uid, tech, missing) = await SendDump(call, done.Tag, done.Reading);
			return DeviceProtocol.NfcResult(key, uid, tech, missing);
		}
		finally { NfcBridge.Instance.EndCall(onTap); }
	}

	/// <summary>
	/// Writes NDEF records to the next tag that is not MIFARE Classic: a Bambu tag is never written,
	/// so every MIFARE Classic tag is refused. It reads a backup first and uploads it, then writes,
	/// then reads back; the result names the read-back.
	/// </summary>
	static async Task<JsonObject> NfcWrite(DeviceCall call)
	{
		var records = DeviceProtocol.WriteRecords(call.Args, out var bad);
		if (records is null) return DeviceProtocol.Failed(bad!);
		NdefMessage message;
		try { message = new NdefMessage(NdefWire.Build(records, "en")); }
		catch (Exception error) { return DeviceProtocol.Failed($"bad_args: {error.Message}"); }
		var wanted = new JsonArray(message.GetRecords()!.Select(r => (JsonNode)NdefWire.Parse(r)).ToArray()).ToJsonString();
		var taps = Taps();
		Action<Tag> onTap = tag => taps.Writer.TryWrite(tag);
		await using var screen = await CallScreen.Show("Write a tag", "Tap the tag to write on the back of the phone. ScadBuddy never writes a Bambu spool's tag.", call);
		NfcBridge.Instance.StartCall(onTap);
		try
		{
			string? written = null;
			// What the last tap did, for the error at the deadline. A MIFARE Classic tap is refused
			// on the screen, and the call waits for another tag.
			var lastTap = DeviceProtocol.WriteTap.None;
			while (true)
			{
				Tag tag;
				try { tag = await taps.Reader.ReadAsync(call.Token); }
				catch (OperationCanceledException)
				{
					if (call.CancelledByPerson) return DeviceProtocol.Failed("cancelled: the person stopped the write on the phone");
					return DeviceProtocol.Failed(DeviceProtocol.WriteTimedOut(written is not null ? DeviceProtocol.WriteTap.Written : lastTap));
				}
				if (NfcBridge.IsClassic(tag))
				{
					lastTap = DeviceProtocol.WriteTap.Classic;
					screen.Say("That is a MIFARE Classic tag, such as a Bambu spool's. ScadBuddy never writes one. Tap another tag.");
					continue;
				}
				var uid = Convert.ToHexString(tag.GetId() ?? []);
				var tech = NfcBridge.TechOf(tag);
				var quiet = new CallProgress(call, null);
				if (written is null)
				{
					lastTap = DeviceProtocol.WriteTap.Lost;
					var before = await Task.Run(() => NfcBridge.ReadTag(tag, quiet));
					if (quiet.LostError is { } lost)
					{
						call.Event(DeviceProtocol.TagLost(0, lost));
						screen.Say("The tag moved before its backup was read. Tap it again.");
						continue;
					}
					// No write without its backup: an upload that fails ends the call.
					var (backup, _, _, _) = await SendDump(call, tag, before);
					call.Event(DeviceProtocol.TagFound(uid, tech, 0, 0, backup));
					// A write needs time for its read-back: it does not start when too little is left.
					if (call.Request.Deadline - DateTimeOffset.UtcNow < WriteNeeds)
						return DeviceProtocol.Failed("timeout: too little time was left to write the tag and read it back");
					screen.Say("Backup kept. Writing.");
					// A blank tag is formatted, and this Tag still lists no Ndef: only the next tap reads it back.
					var formatted = !NfcBridge.HasNdef(tag);
					// The backup took time: a call ended meanwhile writes nothing.
					if (call.Token.IsCancellationRequested) return DeviceProtocol.Failed(DeviceProtocol.WriteStopped(call.CancelledByPerson, call.Ended));
					try { await Task.Run(() => NfcBridge.WriteNdef(tag, message)); }
					catch (NfcBridge.BridgeError error) { return DeviceProtocol.Failed($"refused: {error.Message}"); }
					catch (Exception error)
					{
						call.Event(DeviceProtocol.TagLost(0, $"write: {error.GetType().Name}"));
						screen.Say("The write did not finish. Tap the tag again.");
						continue;
					}
					written = uid;
					if (formatted)
					{
						screen.Say("Written. Tap the tag again to read it back.");
						continue;
					}
				}
				else if (uid != written)
				{
					screen.Say("That is another tag. Tap the tag that was written.");
					continue;
				}
				quiet.LostError = null;
				var after = await Task.Run(() => NfcBridge.ReadTag(tag, quiet));
				if (quiet.LostError is not null)
				{
					call.Event(DeviceProtocol.TagLost(0, $"read back: {quiet.LostError}"));
					screen.Say("Written. Tap the tag again to read it back.");
					continue;
				}
				if ((after["records"] as JsonArray ?? new JsonArray()).ToJsonString() != wanted)
					return DeviceProtocol.Failed("read_back_mismatch: the tag holds other records than were written");
				screen.Say("Written. Sending the read-back.");
				var (key, readUid, readTech, missing) = await SendDump(call, tag, after);
				return DeviceProtocol.NfcResult(key, readUid, readTech, missing);
			}
		}
		finally { NfcBridge.Instance.EndCall(onTap); }
	}

	/// <summary>Uploads a reading as a tag_dump, with no key trailers, and answers its key and what it holds.</summary>
	static async Task<(string Key, string Uid, string Tech, List<int> Missing)> SendDump(DeviceCall call, Tag tag, JsonObject reading)
	{
		var uid = Convert.ToHexString(tag.GetId() ?? []);
		if (uid.Length is < 8 or > 20) throw new CallFailed("uid_unreadable: the tag gave no UID of 4 to 10 bytes");
		var tech = NfcBridge.TechOf(tag);
		var blocks = (reading["mifare"]?["blocks"] as JsonArray ?? new JsonArray())
			.Select(b => (b!["index"]!.GetValue<int>(), b["dataHex"]!.GetValue<string>())).ToList();
		var missing = NfcBridge.IsClassic(tag) ? DeviceProtocol.MissingBlocks(BlockCount(tag), blocks.Select(b => b.Item1)) : [];
		var dump = DeviceProtocol.Dump(uid, tech, blocks, reading["records"] as JsonArray ?? new JsonArray());
		var key = await call.Upload(DeviceProtocol.TagDump, Encoding.UTF8.GetBytes(dump), "application/json");
		return (key, uid, tech, missing);
	}

	static int BlockCount(Tag tag)
	{
		using var classic = MifareClassic.Get(tag);
		return classic?.BlockCount ?? 64;
	}

	/// <summary>A photo with the camera (<c>MediaPicker</c>), uploaded as one scan_image blob.</summary>
	static async Task<JsonObject> Photo(DeviceCall call, string title)
	{
		if (call.Args.Count != 0) return DeviceProtocol.Failed($"bad_args: {call.Method} takes no args");
		FileResult? photo;
		try
		{
			photo = await MainThread.InvokeOnMainThreadAsync(() => MediaPicker.Default.CapturePhotoAsync(new MediaPickerOptions { Title = title }));
		}
		catch (PermissionException) { return DeviceProtocol.Failed("no_camera: the app may not use the camera; allow it in Android's settings"); }
		catch (FeatureNotSupportedException) { return DeviceProtocol.Failed("no_camera: this phone has no camera the app can use"); }
		// The camera cannot be stopped from here: a photo taken after the deadline, or after core
		// ended the call, is not sent.
		if (call.Token.IsCancellationRequested) return DeviceProtocol.Failed("timeout: the photo came after the call's deadline");
		if (photo is null) return DeviceProtocol.Failed("cancelled: no photo was taken");
		await using var stream = await photo.OpenReadAsync();
		using var copy = new MemoryStream();
		var buffer = new byte[81920];
		int n;
		while ((n = await stream.ReadAsync(buffer)) > 0)
		{
			copy.Write(buffer, 0, n);
			if (copy.Length > DeviceProtocol.PhotoMax) return DeviceProtocol.Failed("too_large: the photo is larger than 12 MiB");
		}
		if (call.Token.IsCancellationRequested) return DeviceProtocol.Failed("timeout: the photo came after the call's deadline");
		var type = photo.ContentType ?? "image/jpeg";
		var key = await call.Upload(DeviceProtocol.ScanImage, copy.ToArray(), type);
		return DeviceProtocol.PhotoResult(key, type);
	}

	// The time an nfc.write needs before the deadline: the margin DeviceLink keeps, then the write and a read-back tap.
	static readonly TimeSpan WriteNeeds = TimeSpan.FromSeconds(20);

	// One tap waits at a time; taps while a read runs are dropped.
	static Channel<Tag> Taps() => Channel.CreateBounded<Tag>(new BoundedChannelOptions(1) { FullMode = BoundedChannelFullMode.DropWrite });

	/// <summary>The read's progress as call events and as the screen's text. A null screen says nothing.</summary>
	sealed class CallProgress(DeviceCall call, CallScreen? screen) : ITagProgress
	{
		/// <summary>The last tag_lost's error, null while the read goes on or after it ended well.</summary>
		public string? LostError { get; set; }

		public bool Stopped => call.Token.IsCancellationRequested;

		public void Found(string uid, string tech, int blocks, int resumeFrom)
		{
			if (screen is null) return;
			call.Event(DeviceProtocol.TagFound(uid, tech, blocks, resumeFrom));
			screen.Say(resumeFrom > 0 ? $"Tag found again. Going on from {resumeFrom} of {blocks} blocks." : blocks > 0 ? $"Tag found: {blocks} blocks. Hold it still." : "Tag found.");
		}

		public void Read(int read, int of)
		{
			if (screen is null) return;
			call.Event(DeviceProtocol.TagProgress(read, of));
			screen.Say($"Read {read} of {of} blocks. Hold it still.");
		}

		public void Lost(int read, string error)
		{
			LostError = error;
			if (screen is null) return;
			call.Event(DeviceProtocol.TagLost(read, error));
			screen.Say($"The tag moved after {read} blocks. Tap it again to go on.");
		}
	}
}

/// <summary>A call that ends with this error.</summary>
sealed class CallFailed(string error) : Exception(error);

/// <summary>The full-screen view of a running NFC call: what to do, how it goes, and Cancel.</summary>
sealed class CallScreen : IAsyncDisposable
{
	readonly CallPage page;

	CallScreen(CallPage page) => this.page = page;

	public static Task<CallScreen> Show(string title, string text, DeviceCall call) =>
		MainThread.InvokeOnMainThreadAsync(async () =>
		{
			var page = new CallPage(title, text, call.CancelByPerson);
			if (Application.Current?.Windows.FirstOrDefault()?.Page?.Navigation is { } navigation)
				await navigation.PushModalAsync(page, false);
			return new CallScreen(page);
		});

	public void Say(string text) => MainThread.BeginInvokeOnMainThread(() => page.Status.Text = text);

	public async ValueTask DisposeAsync() =>
		await MainThread.InvokeOnMainThreadAsync(async () =>
		{
			if (page.Navigation.ModalStack.Contains(page)) await page.Navigation.PopModalAsync(false);
		});

	sealed class CallPage : ContentPage
	{
		readonly Action cancel;
		public Label Status { get; }

		public CallPage(string title, string text, Action cancel)
		{
			this.cancel = cancel;
			Title = title;
			Status = new Label { Text = text, FontSize = 22, HorizontalTextAlignment = TextAlignment.Center };
			var stop = new Button { Text = "Cancel" };
			stop.Clicked += (_, _) => cancel();
			Content = new VerticalStackLayout
			{
				Padding = new Thickness(24),
				Spacing = 32,
				VerticalOptions = LayoutOptions.Center,
				Children = { new Label { Text = title, FontSize = 28, HorizontalTextAlignment = TextAlignment.Center }, Status, stop },
			};
		}

		protected override bool OnBackButtonPressed()
		{
			cancel();
			return true;
		}
	}
}
