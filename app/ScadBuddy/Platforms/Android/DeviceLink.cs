using System.Net;
using System.Net.Http.Headers;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading.Channels;

namespace ScadBuddy;

/// <summary>
/// The phone as a paired device (#8244, spec docs/superpowers/specs/2026-11-27-devices-design.md).
/// While the app is in front, it holds ScadBuddy's realtime socket with its device token,
/// subscribed to its own topic <c>device:&lt;id&gt;</c>, and posts a heartbeat every 30 s. A call
/// comes as the bus event <c>device.call_requested</c>; <see cref="DeviceMethods"/> runs it, and
/// its events, uploads and result go back over HTTP with the same token. Both stop in OnPause and
/// start again in OnResume: Android gives NFC to the app in front only.
/// </summary>
public sealed class DeviceLink
{
	public static DeviceLink Instance { get; } = new();

	const string StoreKey = "scadbuddy.device";
	static readonly TimeSpan HeartbeatEvery = TimeSpan.FromSeconds(30);
	// A call's method stops this long before core's deadline, so its upload and result get there in time.
	static readonly TimeSpan Margin = TimeSpan.FromSeconds(5);
	const int FrameMax = 64 << 10;
	static readonly TimeSpan StableFor = TimeSpan.FromMinutes(1);
	static readonly TimeSpan MaxCall = TimeSpan.FromMilliseconds(int.MaxValue - 1);
	// RFC 6455 close code realtime.SignedOut: the pairing ended (revoked, or its token expired).
	const int SignedOut = 4401;

	static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(30) };

	readonly object gate = new();
	Pairing? pairing;
	bool loaded;
	// Whether the app is in front: the socket and the heartbeat run only then.
	bool front;
	CancellationTokenSource? link;
	RunningCall? running;
	// The calls already run: a call is never run twice, even if its frame comes again.
	// A reconnect gets no replay (core's "check" tells only that a topic changed, not its
	// events), so a call published while the socket is down ends at its deadline, offline.
	readonly Queue<string> seen = new();

	/// <summary>The pairing, read from SecureStorage once.</summary>
	async Task<Pairing?> Current()
	{
		lock (gate) if (loaded) return pairing;
		Pairing? stored = null;
		try { stored = DeviceProtocol.FromStored(await SecureStorage.Default.GetAsync(StoreKey)); }
		catch (Exception)
		{
			// A key store that cannot decrypt (a restored backup) has no pairing.
			SecureStorage.Default.Remove(StoreKey);
		}
		lock (gate) { if (!loaded) { pairing = stored; loaded = true; } return pairing; }
	}

	/// <summary>Claims a pairing code and keeps the token. It throws with a message the person can read.</summary>
	public async Task<Pairing> Pair(Uri claim)
	{
		var body = DeviceProtocol.ClaimBody(DeviceInfo.Current.Name, $"{DeviceInfo.Current.Manufacturer} {DeviceInfo.Current.Model}", AppInfo.Current.VersionString);
		using var content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
		using var answer = await Http.PostAsync(claim, content);
		var text = await answer.Content.ReadAsStringAsync();
		if (answer.StatusCode is HttpStatusCode.Gone or HttpStatusCode.Unauthorized)
			throw new InvalidOperationException("That pairing code was used already, or it expired. Make a new one on ScadBuddy's Devices page.");
		if (!answer.IsSuccessStatusCode) throw new InvalidOperationException($"ScadBuddy answered {(int)answer.StatusCode}.");
		var paired = DeviceProtocol.PairingOf(text, claim) ?? throw new InvalidOperationException("ScadBuddy's answer was not a pairing.");
		Close();
		// A call of the old pairing stops, so the new one's calls are not busy.
		lock (gate) running?.End();
		// The phone keeps one pairing: a new one replaces the old, which expires after its idle hour.
		await SecureStorage.Default.SetAsync(StoreKey, DeviceProtocol.Stored(paired));
		lock (gate) { pairing = paired; loaded = true; }
		_ = Open();
		return paired;
	}

	/// <summary>The app came to the front: open the socket and start the heartbeat.</summary>
	public void Resume()
	{
		lock (gate) front = true;
		_ = Open();
	}

	/// <summary>The app went to the back: close the socket and stop the heartbeat. A running call goes on (a photo pauses the app).</summary>
	public void Pause()
	{
		lock (gate) front = false;
		Close();
	}

	async Task Open()
	{
		var p = await Current();
		if (p is null) return;
		if (p.HardCapAt <= DateTimeOffset.UtcNow)
		{
			Ended(p, "This phone's pairing reached its end date. Pair it again from ScadBuddy's Devices page.");
			return;
		}
		CancellationTokenSource cts;
		lock (gate)
		{
			if (!front || link is not null || pairing != p) return;
			link = cts = new CancellationTokenSource();
		}
		_ = Heartbeat(p, cts.Token);
		_ = Socket(p, cts.Token);
	}

	void Close()
	{
		CancellationTokenSource? cts;
		lock (gate) { cts = link; link = null; }
		// Not disposed: the heartbeat and the socket may still read its token as they end.
		cts?.Cancel();
	}

	async Task Heartbeat(Pairing p, CancellationToken stop)
	{
		while (!stop.IsCancellationRequested)
		{
			try
			{
				using var request = Request(p, HttpMethod.Post, "/api/v1/devices/heartbeat");
				using var answer = await Http.SendAsync(request, stop);
				if (answer.StatusCode == HttpStatusCode.Unauthorized)
				{
					Ended(p, null);
					return;
				}
			}
			catch (Exception) when (!stop.IsCancellationRequested)
			{
				// Offline for now: the next beat tries again.
			}
			catch (OperationCanceledException) { return; }
			try { await Task.Delay(HeartbeatEvery, stop); } catch (OperationCanceledException) { return; }
		}
	}

	async Task Socket(Pairing p, CancellationToken stop)
	{
		var wait = TimeSpan.FromSeconds(2);
		while (!stop.IsCancellationRequested)
		{
			using var ws = new ClientWebSocket();
			ws.Options.SetRequestHeader("Authorization", "Bearer " + p.Token);
			ws.Options.CollectHttpResponseDetails = true;
			try
			{
				await ws.ConnectAsync(DeviceProtocol.SocketOf(p.Origin), stop);
				var opened = DateTime.UtcNow;
				await SendText(ws, DeviceProtocol.Subscribe(p.DeviceId), stop);
				await Listen(ws, p, stop);
				// The backoff starts again only after a socket that stayed open: a server that
				// closes at once is not dialled every 2 s.
				if (DateTime.UtcNow - opened >= StableFor) wait = TimeSpan.FromSeconds(2);
				if (ws.CloseStatus is (WebSocketCloseStatus)SignedOut)
				{
					Ended(p, null);
					return;
				}
			}
			catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
			catch (Exception) when (ws.HttpStatusCode == HttpStatusCode.Unauthorized)
			{
				Ended(p, null);
				return;
			}
			catch (Exception) { }
			finally
			{
				if (ws.State == WebSocketState.Open)
					try { await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None); } catch (Exception) { }
			}
			if (stop.IsCancellationRequested) return;
			try { await Task.Delay(wait, stop); } catch (OperationCanceledException) { return; }
			wait = TimeSpan.FromSeconds(Math.Min(wait.TotalSeconds * 2, 30));
		}
	}

	async Task Listen(ClientWebSocket ws, Pairing p, CancellationToken stop)
	{
		var buffer = new byte[8 << 10];
		using var text = new MemoryStream();
		while (ws.State == WebSocketState.Open && !stop.IsCancellationRequested)
		{
			var got = await ws.ReceiveAsync(buffer, stop);
			if (got.MessageType == WebSocketMessageType.Close) return;
			text.Write(buffer, 0, got.Count);
			if (text.Length > FrameMax) throw new InvalidDataException("A realtime frame is too large.");
			if (!got.EndOfMessage) continue;
			var frame = DeviceProtocol.Parse(Encoding.UTF8.GetString(text.GetBuffer(), 0, (int)text.Length), p.DeviceId);
			text.SetLength(0);
			switch (frame)
			{
				case PingFrame:
					await SendText(ws, DeviceProtocol.Pong, stop);
					break;
				case CallRequest call:
					Dispatch(p, call);
					break;
				case CallEnded ended:
					lock (gate) if (running?.Call.Call == ended.Call) running.End();
					break;
			}
		}
	}

	static Task SendText(ClientWebSocket ws, string text, CancellationToken stop) =>
		ws.SendAsync(Encoding.UTF8.GetBytes(text), WebSocketMessageType.Text, true, stop);

	/// <summary>Runs a call, one at a time: a call that comes while another runs ends with <c>busy</c>.</summary>
	void Dispatch(Pairing p, CallRequest call)
	{
		RunningCall? started = null;
		lock (gate)
		{
			if (seen.Contains(call.Call)) return;
			seen.Enqueue(call.Call);
			if (seen.Count > 64) seen.Dequeue();
			if (running is null)
			{
				// The call's context is made here, under the lock, so an end that comes at once
				// (core's call_done, a new pairing) always reaches it.
				var cancel = new CancellationTokenSource();
				running = started = new RunningCall(call, cancel, new DeviceCall(this, p, call, cancel.Token));
			}
		}
		if (started is null)
		{
			_ = Finish(p, call, DeviceProtocol.Failed("busy: another call is running on this phone"));
			return;
		}
		_ = Run(p, started);
	}

	async Task Run(Pairing p, RunningCall run)
	{
		var call = run.Call;
		using var span = Telemetry.Source.StartActivity("device.call");
		span?.SetTag("device.method", call.Method);
		var until = call.Deadline - Margin - DateTimeOffset.UtcNow;
		// CancelAfter takes at most int.MaxValue ms (about 24 days); core's deadlines are at most 2 minutes.
		run.Cancel.CancelAfter(until <= TimeSpan.Zero ? TimeSpan.Zero : until > MaxCall ? MaxCall : until);
		var context = run.Context;
		JsonObject result;
		try { result = await DeviceMethods.Run(context); }
		catch (Exception error) when (error is CallFailed or UploadFailed) { result = DeviceProtocol.Failed(error.Message); }
		// An upload stopped by a Cancel, by core, or by a new pairing.
		catch (OperationCanceledException) when (context.Token.IsCancellationRequested)
		{
			result = DeviceProtocol.Failed(DeviceProtocol.CallStopped(context.CancelledByPerson, context.Ended));
		}
		catch (Exception error) { result = DeviceProtocol.Failed($"failed: {error.GetType().Name}"); }
		await context.Flush();
		var outcome = result["outcome"]?.GetValue<string>() ?? "error";
		span?.SetTag("device.outcome", outcome);
		if (result["error"]?.GetValue<string>() is { } failure)
		{
			// The error's code only: an exception's text could name a tag.
			span?.SetTag("device.error", DeviceProtocol.CodeOf(failure));
			span?.SetStatus(System.Diagnostics.ActivityStatusCode.Error, DeviceProtocol.CodeOf(failure));
		}
		var sent = await Finish(p, call, result);
		span?.SetTag("device.result_sent", sent);
		lock (gate) if (running == run) running = null;
		run.Cancel.Dispose();
	}

	/// <summary>Posts a call's result. It says whether core took it.</summary>
	async Task<bool> Finish(Pairing p, CallRequest call, JsonObject result)
	{
		try
		{
			using var request = Request(p, HttpMethod.Post, $"/api/v1/devices/calls/{call.Call}/result");
			request.Content = new StringContent(result.ToJsonString(), Encoding.UTF8, "application/json");
			using var answer = await Http.SendAsync(request);
			if (answer.StatusCode == HttpStatusCode.Unauthorized) Ended(p, null);
			return answer.IsSuccessStatusCode;
		}
		catch (Exception) { return false; }
	}

	internal static HttpRequestMessage Request(Pairing p, HttpMethod method, string path)
	{
		var request = new HttpRequestMessage(method, new Uri(new Uri(p.Origin), path));
		request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", p.Token);
		return request;
	}

	internal static HttpClient Client => Http;

	/// <summary>The pairing ended (401, or close code 4401): forget the token and tell the person.</summary>
	internal void Ended(Pairing p, string? why)
	{
		lock (gate)
		{
			if (pairing != p) return;
			pairing = null;
		}
		Close();
		SecureStorage.Default.Remove(StoreKey);
		NfcBridge.Notify(why ?? "This phone's pairing with ScadBuddy ended. Pair it again from ScadBuddy's Devices page.");
	}

	sealed class RunningCall(CallRequest call, CancellationTokenSource cancel, DeviceCall context)
	{
		public CallRequest Call { get; } = call;
		public CancellationTokenSource Cancel { get; } = cancel;
		public DeviceCall Context { get; } = context;

		/// <summary>Core ended the call, or a new pairing replaced its own: stop it, uploads too.</summary>
		public void End() => Context.End();
	}
}

/// <summary>
/// One running call as <see cref="DeviceMethods"/> sees it: its args, a token that fires before
/// its deadline (or when core ends it, or the person cancels it), its events in order, and its uploads.
/// </summary>
public sealed class DeviceCall
{
	readonly DeviceLink link;
	readonly Pairing pairing;
	readonly Channel<JsonObject> events = Channel.CreateUnbounded<JsonObject>(new UnboundedChannelOptions { SingleReader = true });
	readonly Task pump;
	readonly CancellationTokenSource cancel;
	static readonly HttpClient Uploads = new() { Timeout = Timeout.InfiniteTimeSpan };
	readonly CancellationTokenSource ended = new();

	internal DeviceCall(DeviceLink link, Pairing pairing, CallRequest call, CancellationToken token)
	{
		this.link = link;
		this.pairing = pairing;
		Request = call;
		cancel = CancellationTokenSource.CreateLinkedTokenSource(token);
		pump = Pump();
	}

	public CallRequest Request { get; }
	public string Method => Request.Method;
	public JsonObject Args => Request.Args;
	public CancellationToken Token => cancel.Token;
	/// <summary>Whether the person stopped the call on the phone.</summary>
	public bool CancelledByPerson { get; private set; }

	public void CancelByPerson()
	{
		CancelledByPerson = true;
		End();
	}

	/// <summary>Queues an event; they are posted in order, a few at a time.</summary>
	public void Event(JsonObject e) => events.Writer.TryWrite(e);

	async Task Pump()
	{
		var batch = new List<JsonObject>();
		while (await events.Reader.WaitToReadAsync())
		{
			batch.Clear();
			while (batch.Count < DeviceProtocol.EventsPerPost && events.Reader.TryRead(out var e)) batch.Add(e);
			foreach (var body in DeviceProtocol.EventBodies(batch))
			{
				try
				{
					using var request = DeviceLink.Request(pairing, HttpMethod.Post, $"/api/v1/devices/calls/{Request.Call}/events");
					request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
					using var answer = await DeviceLink.Client.SendAsync(request);
					// 409: core ended the call (its deadline, or a revoke): stop the method.
					if (answer.StatusCode == HttpStatusCode.Conflict) End();
					if (answer.StatusCode == HttpStatusCode.Unauthorized) { End(); link.Ended(pairing, null); }
				}
				catch (Exception)
				{
					// An event that does not get there is lost; the result still tells the end.
				}
			}
		}
	}

	/// <summary>Posts every queued event, before the result.</summary>
	internal async Task Flush()
	{
		events.Writer.TryComplete();
		await pump;
		cancel.Dispose();
		ended.Dispose();
	}

	/// <summary>
	/// Ends the call before its deadline: the person cancelled it, core ended it, or its pairing
	/// went. Unlike the deadline's margin, this stops an upload in flight too.
	/// </summary>
	internal void End()
	{
		try
		{
			ended.Cancel();
			cancel.Cancel();
		}
		catch (ObjectDisposedException) { }
	}

	/// <summary>Whether the call was ended before its deadline (<see cref="End"/>).</summary>
	public bool Ended => ended.IsCancellationRequested;

	/// <summary>Uploads what the call read as one blob of <paramref name="kind"/>, and answers its key.</summary>
	public async Task<string> Upload(string kind, byte[] body, string mediaType)
	{
		using var request = DeviceLink.Request(pairing, HttpMethod.Post, $"/api/v1/devices/uploads?call={Request.Call}&kind={kind}");
		request.Content = new ByteArrayContent(body);
		request.Content.Headers.ContentType = new MediaTypeHeaderValue(mediaType);
		// A photo on a slow link takes longer than the shared client's 30 s, and core takes the
		// upload only until the call's deadline.
		var left = Request.Deadline - DateTimeOffset.UtcNow;
		using var until = CancellationTokenSource.CreateLinkedTokenSource(ended.Token);
		until.CancelAfter(left > TimeSpan.FromSeconds(1) ? left : TimeSpan.FromSeconds(1));
		using var answer = await Uploads.SendAsync(request, until.Token);
		if (answer.StatusCode == HttpStatusCode.Unauthorized) link.Ended(pairing, null);
		if (!answer.IsSuccessStatusCode) throw new UploadFailed((int)answer.StatusCode);
		var key = (JsonNode.Parse(await answer.Content.ReadAsStringAsync()) as JsonObject)?["key"]?.GetValue<string>();
		return key ?? throw new UploadFailed((int)answer.StatusCode);
	}
}

/// <summary>An upload core did not take.</summary>
public sealed class UploadFailed(int status) : Exception($"upload_failed: ScadBuddy answered {status}")
{
	public int Status { get; } = status;
}
