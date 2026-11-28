using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;

namespace ScadBuddy;

/// <summary>
/// The open flow questions this phone follows (#8574, spec
/// docs/superpowers/specs/2026-11-28-native-task-screens-design.md §6.3), read over core's
/// device task routes with the device token. <see cref="DeviceLink"/> calls <see cref="Reload"/>
/// on every connect, every resync and every few minutes, and <see cref="OnChange"/> for each
/// <c>task.changed</c>. A task newly open raises <see cref="Arrived"/> once per id in a
/// process, so a reconnect never notifies twice. A server without the routes (404) has no
/// tasks; a 403 <c>device_tasks_off</c> empties the list and notifies nothing.
/// Nothing here logs a task's title, schema, context or answer.
/// </summary>
public sealed class TaskInbox
{
	public static TaskInbox Instance { get; } = new();

	const string Tasks = "/api/v1/devices/tasks";
	const string One = "/api/v1/devices/task";
	const string AnswerPath = "/api/v1/devices/task-answer";
	const string PhotoPath = "/api/v1/devices/task-photo";
	const string ScanPath = "/api/v1/devices/task-scan";
	const string ImagePath = "/api/v1/devices/task-image";

	// A photo on a slow link takes longer than the shared client's 30 s.
	static readonly HttpClient Uploads = new() { Timeout = TimeSpan.FromMinutes(2) };

	readonly object gate = new();
	readonly SemaphoreSlim reading = new(1, 1);
	List<DeviceTask> open = [];
	// The ids already announced: a task notifies once per process.
	readonly HashSet<string> announced = [];

	/// <summary>The open tasks, oldest first.</summary>
	public IReadOnlyList<DeviceTask> Open { get { lock (gate) return open.ToList(); } }

	/// <summary>Whether core said this phone may not answer tasks (the setting, or this device's switch).</summary>
	public bool Off { get; private set; }

	/// <summary>The list changed.</summary>
	public event Action? Changed;

	/// <summary>A task newly open, with its context cards: once per id in this process.</summary>
	public event Action<DeviceTask>? Arrived;

	/// <summary>A task left the list (answered, cancelled, expired, or no longer this phone's).</summary>
	public event Action<string>? Left;

	/// <summary>Reads the list again. It answers false when the list could not be read (offline, a server error).</summary>
	public async Task<bool> Reload()
	{
		var p = await DeviceLink.Instance.PairingNow();
		if (p is null) return false;
		await reading.WaitAsync();
		try
		{
			using var request = DeviceLink.Request(p, HttpMethod.Get, Tasks);
			using var answer = await DeviceLink.Client.SendAsync(request);
			var body = await answer.Content.ReadAsStringAsync();
			if (answer.StatusCode == HttpStatusCode.Unauthorized)
			{
				DeviceLink.Instance.Ended(p, null);
				return false;
			}
			if (answer.StatusCode == HttpStatusCode.Forbidden && TaskForm.TasksOff(body))
			{
				Off = true;
				Replace([]);
				return true;
			}
			// A ScadBuddy without the task routes yet: no tasks, nothing to notify.
			if (answer.StatusCode == HttpStatusCode.NotFound)
			{
				Off = false;
				Replace([]);
				return true;
			}
			if (!answer.IsSuccessStatusCode || TaskForm.ListOf(body) is not { } tasks) return false;
			Off = false;
			// The list has no context cards: a new task is read whole for its notification.
			var fresh = new List<DeviceTask>();
			foreach (var task in tasks.Where(t => t.State == "open"))
			{
				bool known;
				lock (gate) known = open.Any(o => o.Id == task.Id);
				fresh.Add(known ? task : await Read(p, task.Id) ?? task);
			}
			Replace(fresh);
			return true;
		}
		catch (Exception)
		{
			// Offline: the list stays as it was, and the next connect reads it again.
			return false;
		}
		finally { reading.Release(); }
	}

	/// <summary>A task.changed frame: the task is read again, or dropped when it is not open.</summary>
	public async Task OnChange(string id, string state)
	{
		if (state != "open")
		{
			Remove(id);
			return;
		}
		var p = await DeviceLink.Instance.PairingNow();
		if (p is null) return;
		DeviceTask? task;
		try { task = await Read(p, id); }
		catch (Exception) { return; }
		// Not this phone's (another person's, another place) or gone meanwhile.
		if (task is null || task.State != "open")
		{
			Remove(id);
			return;
		}
		DeviceTask? arrived = null;
		lock (gate)
		{
			var at = open.FindIndex(o => o.Id == id);
			if (at >= 0) open[at] = task;
			else
			{
				open.Add(task);
				if (announced.Add(id)) arrived = task;
			}
		}
		Changed?.Invoke();
		if (arrived is not null) Arrived?.Invoke(arrived);
	}

	/// <summary>One task, with its context cards, or null when this phone does not follow it.</summary>
	public async Task<DeviceTask?> Get(string id)
	{
		var p = await DeviceLink.Instance.PairingNow();
		if (p is null) return null;
		var task = await Read(p, id);
		if (task is null || task.State != "open") Remove(id);
		return task;
	}

	async Task<DeviceTask?> Read(Pairing p, string id)
	{
		using var request = DeviceLink.Request(p, HttpMethod.Get, $"{One}?id={Uri.EscapeDataString(id)}");
		using var answer = await DeviceLink.Client.SendAsync(request);
		if (answer.StatusCode == HttpStatusCode.Unauthorized) DeviceLink.Instance.Ended(p, null);
		if (!answer.IsSuccessStatusCode) return null;
		return TaskForm.OneOf(await answer.Content.ReadAsStringAsync());
	}

	/// <summary>
	/// Sends an answer with its idempotency key, the same on every try of one answer. Status 0
	/// is no answer from ScadBuddy (offline). 200 and 202 took it; 409 means the task was
	/// answered elsewhere; 422 carries the problems. A task that took an answer leaves the list.
	/// </summary>
	public async Task<AnswerResult> Answer(string id, JsonObject answer, string idempotencyKey)
	{
		var p = await DeviceLink.Instance.PairingNow();
		if (p is null) return new AnswerResult(401, []);
		try
		{
			using var request = DeviceLink.Request(p, HttpMethod.Post, $"{AnswerPath}?id={Uri.EscapeDataString(id)}");
			request.Headers.Add("Idempotency-Key", idempotencyKey);
			request.Content = new StringContent(new JsonObject { ["answer"] = answer.DeepClone() }.ToJsonString(), Encoding.UTF8, "application/json");
			using var reply = await DeviceLink.Client.SendAsync(request);
			var status = (int)reply.StatusCode;
			if (status == 401) DeviceLink.Instance.Ended(p, null);
			if (status is 200 or 202 or 409 or 404) Remove(id);
			if (status == 403 && TaskForm.TasksOff(await reply.Content.ReadAsStringAsync()))
			{
				Off = true;
				Replace([]);
			}
			var problems = status == 422 ? TaskForm.ProblemsOf(await reply.Content.ReadAsStringAsync()) : [];
			return new AnswerResult(status, problems);
		}
		catch (Exception)
		{
			return new AnswerResult(0, []);
		}
	}

	/// <summary>Uploads a photo for a photo field and answers its attachment id. It throws with a message the person can read.</summary>
	public async Task<string> UploadPhoto(string id, byte[] photo, string mediaType)
	{
		var p = await DeviceLink.Instance.PairingNow() ?? throw new InvalidOperationException("This phone is not paired.");
		using var request = DeviceLink.Request(p, HttpMethod.Post, $"{PhotoPath}?id={Uri.EscapeDataString(id)}");
		request.Content = new ByteArrayContent(photo);
		request.Content.Headers.ContentType = new MediaTypeHeaderValue(mediaType);
		using var reply = await Uploads.SendAsync(request);
		if (reply.StatusCode == HttpStatusCode.Unauthorized) DeviceLink.Instance.Ended(p, null);
		if (!reply.IsSuccessStatusCode) throw new InvalidOperationException($"The photo was not taken: ScadBuddy answered {(int)reply.StatusCode}.");
		return TaskForm.AttachmentOf(await reply.Content.ReadAsStringAsync()) ?? throw new InvalidOperationException("ScadBuddy's answer named no photo.");
	}

	/// <summary>Sends what the phone read for a scan or entity field: a tag's dump, or a photo of a code. It throws with a message the person can read.</summary>
	public async Task<ScanResult> Scan(string id, string field, string? tagDump, byte[]? image, string? mediaType)
	{
		var p = await DeviceLink.Instance.PairingNow() ?? throw new InvalidOperationException("This phone is not paired.");
		using var request = DeviceLink.Request(p, HttpMethod.Post, $"{ScanPath}?id={Uri.EscapeDataString(id)}&field={Uri.EscapeDataString(field)}");
		if (tagDump is not null)
			request.Content = new StringContent($$"""{"tag":{{tagDump}}}""", Encoding.UTF8, "application/json");
		else
		{
			request.Content = new ByteArrayContent(image ?? []);
			request.Content.Headers.ContentType = new MediaTypeHeaderValue(mediaType ?? "image/jpeg");
		}
		using var reply = await Uploads.SendAsync(request);
		if (reply.StatusCode == HttpStatusCode.Unauthorized) DeviceLink.Instance.Ended(p, null);
		if (!reply.IsSuccessStatusCode) throw new InvalidOperationException($"The read was not looked up: ScadBuddy answered {(int)reply.StatusCode}.");
		return TaskForm.ScanOf(await reply.Content.ReadAsStringAsync()) ?? throw new InvalidOperationException("ScadBuddy's answer was not a lookup.");
	}

	/// <summary>An image block's bytes, or null.</summary>
	public async Task<byte[]?> Image(string id, string attachment)
	{
		var p = await DeviceLink.Instance.PairingNow();
		if (p is null) return null;
		try
		{
			using var request = DeviceLink.Request(p, HttpMethod.Get, $"{ImagePath}?id={Uri.EscapeDataString(id)}&attachment={Uri.EscapeDataString(attachment)}");
			using var reply = await Uploads.SendAsync(request);
			return reply.IsSuccessStatusCode ? await reply.Content.ReadAsByteArrayAsync() : null;
		}
		catch (Exception) { return null; }
	}

	/// <summary>The pairing ended or changed: no tasks, and their notifications go.</summary>
	public void Clear()
	{
		Replace([]);
		lock (gate) announced.Clear();
	}

	void Replace(List<DeviceTask> tasks)
	{
		List<string> gone;
		List<DeviceTask> arrived = [];
		lock (gate)
		{
			gone = open.Where(o => tasks.All(t => t.Id != o.Id)).Select(o => o.Id).ToList();
			foreach (var task in tasks)
				if (announced.Add(task.Id)) arrived.Add(task);
			open = tasks;
		}
		Changed?.Invoke();
		foreach (var id in gone) Left?.Invoke(id);
		foreach (var task in arrived) Arrived?.Invoke(task);
	}

	void Remove(string id)
	{
		bool removed;
		lock (gate) removed = open.RemoveAll(o => o.Id == id) > 0;
		if (!removed) return;
		Changed?.Invoke();
		Left?.Invoke(id);
	}
}

/// <summary>An answer's reply: its HTTP status (0: no reply), and a 422's problems by field.</summary>
public sealed record AnswerResult(int Status, IReadOnlyList<(string Field, string Message)> Problems);
